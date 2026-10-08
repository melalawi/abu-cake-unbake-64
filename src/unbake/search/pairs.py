"""One measured pair/frontier executor for source and option episodes."""

from __future__ import annotations

import hashlib
import math
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from unbake.compilers.drivers import resolved
from unbake.compilers.options import classify_capability, infer_options, plan_episode
from unbake.compilers.ranking import measured_candidate_rank
from unbake.compilers.recipe_options import recipe_digest
from unbake.config import Held, Host, Project
from unbake.inputs import DependencySet
from unbake.layout import split
from unbake.search.frontier import retain
from unbake.work import attempts
from unbake.work.compare import Compared, measure, operation_dependencies, row_of, view_for
from unbake.work.source_scope import SourceScope, materialize


@dataclass(frozen=True)
class Candidate:
    source: str
    path: Path
    trial: Compared
    project: Project
    identity: str
    kind: str

    @property
    def rank(self) -> tuple[bool, int, int, float]:
        return measured_candidate_rank(self.trial.compares, self.trial.best_percent)


class Pairs:
    def __init__(self, project: Project, host: Host, scope: SourceScope, write: Callable[[dict[str, Any]], None]):
        self.project, self.host, self.scope, self.write = project, host, scope, write
        self.versions = split.holding_versions(project, scope.subject)
        self.targets = {
            v: hashlib.sha256(split.words(project, row_of(project, scope.subject, v))).hexdigest()
            for v in self.versions
        }
        self.cache: dict[str, Candidate | None] = {}
        self.candidates: dict[str, Candidate] = {}
        self.score_cache: dict[str, Any] = {}
        self.counters = {"attempted": 0, "measured": 0, "pair_cache_hits": 0, "option_pairs": 0}
        self.capabilities: dict[str, list[dict[str, Any]]] = {}
        self.baseline: Candidate | None = None

    def evaluate(
        self, content: str, kind: str, description: str, view: Project, *, baseline: Compared | None = None
    ) -> Candidate | None:
        # Reuse the baseline path for an unchanged source, so its complete input
        # identity can be checked before any reuse. Other sources are durable.
        path = (
            baseline.file
            if baseline is not None
            else (
                self.baseline.path
                if self.baseline is not None and content == self.baseline.source
                else materialize(self.project, self.scope, content)
            )
        )
        view = replace(
            view, source_bindings=tuple(dict.fromkeys((*view.source_bindings, (str(path.resolve()), self.scope.unit))))
        )
        view = view_for(view, path, self.scope.subject)
        self.counters["attempted"] += 1
        fault, candidate, cached = None, None, False
        requested = {
            "source": hashlib.sha256(content.encode()).hexdigest(),
            "recipe": view.recipe_for(self.scope.unit).document(),
        }
        identity = recipe_digest(requested)
        try:
            recipes = {v: resolved(view, v, self.scope.unit).digest for v in self.versions}
            dependencies = operation_dependencies(view, self.host, path)
            identity = recipe_digest(
                {
                    "source": requested["source"],
                    "recipes": recipes,
                    "targets": self.targets,
                    "dependencies": dependencies.digest,
                }
            )
            cached = identity in self.cache
            if cached:
                self.counters["pair_cache_hits"] += 1
                candidate = self.cache[identity]
            else:
                reusable = (
                    baseline is not None
                    and baseline.source_sha256 == requested["source"]
                    and set(baseline.compares) == set(self.versions)
                    and all(
                        m.provenance.get("recipe_digest") == recipes[v]
                        and m.provenance.get("dependency_digest") == dependencies.digest
                        and m.provenance.get("target_sha256") == self.targets[v]
                        for v, m in baseline.compares.items()
                    )
                )
                from unbake.compilers.families import family_for

                trial = (
                    baseline
                    if reusable
                    else family_for(view.compiler_reference(self.scope.unit)).capability_probe(
                        lambda: measure(view, self.host, path, retain_link_faults=True, score_cache=self.score_cache)
                    )
                )
                assert trial is not None
                from unbake.work.compare_facts import attach

                attach(view, trial)
                candidate = Candidate(content, path, trial, view, identity, kind)
                self.cache[identity] = candidate
                self.candidates[identity] = candidate
                self.capabilities[identity] = [
                    classify_capability(recipes[v], m, trial.faults.get(v)).document()
                    for v, m in trial.compares.items()
                ]
                if not reusable:
                    self.counters["measured"] += 1
                    row = row_of(self.project, trial.function, self.versions[0])
                    observed = attempts.Attempt(
                        attempts.now(),
                        trial.function,
                        trial.source_sha256,
                        row.end - row.start,
                        trial.document()["versions"],
                        trial.best_percent,
                        trial.exact,
                        trial.seconds,
                        trial.compiler,
                    )
                    attempts.ledger(self.project).note(
                        "compare",
                        trial.function,
                        {
                            **trial.document(),
                            "attempt": observed.document(),
                            "pair_identity": identity,
                            "strategy": kind,
                            "capabilities": self.capabilities[identity],
                        },
                        dependencies=dependencies,
                    )
        except Held as failure:
            fault = failure.fault
            self.cache[identity] = None
            self.capabilities[identity] = [classify_capability(identity, fault=fault.document()).document()]
            attempts.ledger(self.project).note(
                "search.refusal",
                self.scope.subject,
                {
                    "pair_identity": identity,
                    "source": str(path),
                    "recipe": requested["recipe"],
                    "capabilities": self.capabilities[identity],
                },
                state="blocked",
                fault=fault,
                dependencies=DependencySet((), {"requested": requested, "dependencies_unknown": True}, {}),
            )
        self.write(
            {
                "identity": identity,
                "kind": kind,
                "description": description,
                "cached": cached,
                "refusal": fault.document() if fault else None,
                "recipe": requested["recipe"],
                "capabilities": self.capabilities.get(identity, []),
                "result": candidate.trial.document() if candidate else None,
            }
        )
        if self.baseline is None and candidate is not None:
            self.baseline = candidate
        return candidate

    def active(self) -> list[Candidate]:
        assert self.baseline is not None
        return retain(list(self.candidates.values()), self.host.search_frontier, self.baseline)

    def best(self) -> Candidate:
        return min(self.candidates.values(), key=lambda row: (row.rank, row.identity))

    def episode(self, parents: list[Candidate], deadline: float = math.inf) -> dict[str, Any]:
        capabilities = []
        stop = "finite_plan"
        for parent in parents[: self.host.episode_parents]:
            episode = plan_episode(
                parent.project,
                self.scope.unit,
                evidence=infer_options(parent.trial.facts),
                recipe_limit=self.host.episode_recipes,
                parent_limit=self.host.episode_parents,
            )
            for recipe in episode.recipes:
                if time.monotonic() >= deadline:
                    stop = "deadline_after_start"
                    break
                view = replace(parent.project, units={**parent.project.units, self.scope.unit: recipe})
                self.counters["option_pairs"] += 1
                candidate = self.evaluate(parent.source, parent.kind, "bounded option episode", view)
                if candidate is not None:
                    capabilities.extend(self.capabilities[candidate.identity])
                if self.best().trial.exact:
                    stop = "exact"
                    break
            if stop != "finite_plan":
                break
        return {
            "capabilities": capabilities,
            "stop_reason": stop,
            "parents": [p.identity for p in parents[: self.host.episode_parents]],
        }


def option_episode(project: Project, host: Host, source: Path, baseline: Compared) -> Compared:
    """Explicit flags enter the same pair measurement/history/frontier owner."""
    from unbake.work.source_scope import admit_source

    scope = admit_source(project, source)
    rows: list[dict[str, Any]] = []
    pairs = Pairs(project, host, scope, rows.append)
    initial = pairs.evaluate(source.read_text(), "baseline", "supplied comparison", project, baseline=baseline)
    if initial is None:
        return baseline
    episode = pairs.episode(pairs.active())
    result = pairs.best().trial
    result.option_episode = {
        **episode,
        "frontier": [p.identity for p in pairs.active()],
        "pairs": rows,
        "counters": pairs.counters,
    }
    return result
