"""Measured source and recipe frontier with finite option episodes."""

from __future__ import annotations

import json
import math
import re
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from unbake import atomic as atomic_files
from unbake.compilers.families.types import Allocation
from unbake.config import Held, Host, Project
from unbake.decomp import explain
from unbake.process import named as cause_named
from unbake.process import read_text
from unbake.work.compare import Compared


@dataclass(frozen=True)
class Mutation:
    kind: str
    description: str
    source: str


class Generator(Protocol):
    def propose(self, source: str, trial: Compared, ctx: Context) -> Iterable[Mutation]: ...


@dataclass(frozen=True)
class Context:
    project: Project
    policy: Host
    out: Path
    source: Path
    allocation: Allocation
    focus_lines: tuple[int, ...]
    deadline: float
    compiler_facts: dict[str, Any] | None = None


@dataclass(frozen=True)
class SearchResult:
    source: Path
    trial: Compared
    trials: int
    steps: Path
    skips: tuple[dict[str, str], ...] = ()
    stop_reason: str = "finite_plan"
    frontier: tuple[dict[str, Any], ...] = ()
    telemetry: dict[str, Any] = field(default_factory=dict)
    hints: tuple[dict[str, Any], ...] = ()


def _positive(policy: Host, name: str) -> int:
    value = getattr(policy, name, None)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise Held(
            cause_named(
                f"policy.{name}", f"policy.{name}: positive integer required", owner="search.core", stage="search"
            )
        )
    return value


def preprocess(project: Project, policy: Host, source: Path, version: str, deadline: float) -> str:
    """Use the selected unit's build preprocessor, includes and VERSION flags."""
    from unbake.compilers import drivers

    command = drivers.preprocess_command(project, str(policy.cpp), version, source.stem, source, non_matching=True)
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise Held(
            cause_named(
                "context.deadline",
                "context.deadline: preprocessing budget exhausted",
                owner="search.core",
                stage="search",
            )
        )
    output = drivers.run_preprocess(
        project, command, "preprocess", unit=source, context={"source": str(source), "version": version}
    )
    expanded = re.sub(r"^\s*#\s*(?:line\s+)?\d+[^\n]*", "", output, flags=re.M)
    # Preprocessors discard comments; retain explicit source evidence in mutations.
    comments = re.findall(r'/\*.*?\*/|//[^\n]*|"(?:\\.|[^"\\])*"', source.read_text(), re.S)
    markers = [comment for comment in comments if comment.startswith("/") and "FAKEMATCH:" in comment]
    return "".join(comment + "\n" for comment in markers if comment not in expanded) + expanded


def _focus_lines(allocation: Allocation, original: str, expanded: str) -> tuple[int, ...]:
    candidates = {number for difference in allocation.differences for number in difference.candidates}
    lines = {line for pseudo in allocation.pseudos if pseudo.number in candidates for line in pseudo.source_lines}
    source_lines = original.splitlines()
    expressions = {source_lines[line - 1].strip() for line in lines if 1 <= line <= len(source_lines)}
    return tuple(index for index, text in enumerate(expanded.splitlines(), 1) if text.strip() in expressions)


def run(
    project: Project,
    policy: Host,
    source: Path,
    generators: Iterable[Generator],
    out: Path,
    budget_seconds: float,
    *,
    external_roots: tuple[Path, ...] = (),
) -> SearchResult:
    from unbake.compilers.recipe_options import recipe_digest
    from unbake.search.frontier import coordinates
    from unbake.search.pairs import Candidate, Pairs
    from unbake.search.permute import Permuter
    from unbake.search.validate import validate_mutation
    from unbake.work.source_scope import admit_source

    width = _positive(policy, "search_frontier")
    if width < 4:
        raise Held(
            cause_named("search.frontier", "minimum frontier width is four", owner="search.core", stage="search")
        )
    if type(budget_seconds) not in (int, float) or not math.isfinite(budget_seconds) or budget_seconds <= 0:
        raise Held(
            cause_named("budget_seconds", "positive finite budget required", owner="search.core", stage="search")
        )
    generators = list(generators)
    if not generators or any(not callable(getattr(g, "propose", None)) for g in generators):
        raise Held(
            cause_named("generators", "applicable source generators required", owner="search.core", stage="search")
        )
    source = Path(source).resolve()
    scope = admit_source(project, source, external_roots=external_roots)
    text = read_text(source, "search")
    out.mkdir(parents=True, exist_ok=True)
    steps = out / "steps.jsonl"
    deadline = time.monotonic() + budget_seconds
    prepared: dict[tuple[str, str, str], tuple[str, Allocation, tuple[int, ...]] | None] = {}
    skips: list[dict[str, str]] = []
    episodes: set[str] = set()

    def write(row: dict[str, Any]) -> None:
        with atomic_files.stream(steps, "a", encoding="utf-8") as stream:
            stream.write(json.dumps(row, sort_keys=True, allow_nan=False) + "\n")

    pairs = Pairs(project, policy, scope, write)
    counters = pairs.counters
    evaluate = pairs.evaluate

    def prepare(
        parent: Candidate, generator: Generator, version: str
    ) -> tuple[str, Allocation, tuple[int, ...]] | None:
        mode = (
            "facts"
            if getattr(generator, "needs_compiler_facts", False)
            else "allocation"
            if getattr(generator, "needs_allocation", False)
            else "preprocess"
            if getattr(generator, "needs_preprocess", False)
            else "source"
        )
        key = parent.identity, version, mode
        if key not in prepared:
            try:
                expanded = (
                    preprocess(parent.project, policy, parent.path, version, deadline)
                    if mode != "source"
                    else parent.source
                )
                allocation = Allocation((), (), (), ())
                if mode == "allocation":
                    allocation = explain.allocation(parent.project, policy, parent.path, version)
                if mode == "facts":
                    from unbake.work.compare_dump import collect
                    from unbake.work.compare_facts import attach

                    attach(parent.project, parent.trial)
                    collect(parent.project, policy, parent.trial)
                prepared[key] = expanded, allocation, _focus_lines(allocation, parent.source, expanded)
            except Held as failure:
                row = {"key": failure.key, "reason": failure.reason, "version": version, "identity": parent.identity}
                skips.append(row)
                write({"kind": "preparation.refusal", **row})
                prepared[key] = None
        return prepared[key]

    initial = evaluate(text, "baseline", "starting source and recipe", project)
    if initial is None:
        raise Held(
            cause_named("search.initial", f"initial trial failed; see {steps}", owner="search.core", stage="search")
        )
    best, active = initial, [initial]
    stop_reason = "finite_plan"
    visited: set[tuple[str, str]] = set()
    while time.monotonic() < deadline and not best.trial.exact and not pairs.exhausted:
        prior = {row.identity for row in active}
        for parent in tuple(active):
            representative = min(
                parent.trial.compares, key=lambda v: (parent.trial.compares[v].identical_words or 0, v)
            )
            for generator in generators:
                method = getattr(generator, "name", type(generator).__name__)
                route = parent.identity, method
                if route in visited:
                    continue
                if time.monotonic() >= deadline:
                    break
                visited.add(route)
                version = generator.version if isinstance(generator, Permuter) else representative
                preparation = prepare(parent, generator, version)
                if preparation is None:
                    continue
                expanded, allocation, focus_lines = preparation
                context = Context(
                    parent.project,
                    policy,
                    out,
                    parent.path,
                    allocation,
                    focus_lines,
                    deadline,
                    parent.trial.facts.get(version) if getattr(generator, "needs_compiler_facts", False) else None,
                )
                try:
                    for mutation in generator.propose(expanded, parent.trial, context):
                        if time.monotonic() >= deadline or pairs.exhausted:
                            break
                        try:
                            content = validate_mutation(parent.source, mutation.source, scope.subject)
                        except ValueError as error:
                            write({"kind": "mutation.refusal", "method": method, "reason": str(error)})
                            continue
                        kind = (
                            "composition"
                            if {parent.kind, mutation.kind} == {"conversion-scope", "tail-duplicate"}
                            else mutation.kind
                        )
                        candidate = evaluate(content, kind, mutation.description, parent.project)
                        if candidate is not None and candidate.rank < best.rank:
                            best = candidate
                        if best.trial.exact:
                            break
                except Held as failure:
                    skips.append({"key": failure.key, "reason": failure.reason, "generator": method})
                if best.trial.exact or pairs.exhausted:
                    break
            if best.trial.exact or pairs.exhausted:
                break
        active = pairs.active()
        plateau = prior == {row.identity for row in active}
        if plateau and not best.trial.exact and not pairs.exhausted:
            episode_key = recipe_digest({"parents": sorted(row.identity for row in active), "targets": pairs.targets})
            if episode_key in episodes:
                break
            episodes.add(episode_key)
            episode = pairs.episode(active, deadline)
            best = pairs.best()
            write({"kind": "option.episode", **episode})
            active = pairs.active()
            if prior == {row.identity for row in active}:
                break
    if best.trial.exact:
        stop_reason = "exact"
    elif pairs.exhausted:
        stop_reason = "no_gain_handoff"
    elif time.monotonic() >= deadline:
        stop_reason = "deadline_after_start"
    elif skips and counters["measured"] == 1:
        stop_reason = "missing_prerequisite"
    telemetry = {
        "search": counters,
        "external": [getattr(g, "telemetry", None) for g in generators if isinstance(g, Permuter)],
    }
    from unbake.work.hints import proven_techniques

    hints = proven_techniques(best.trial.facts, pairs.no_gain_probes)
    frontier = tuple(
        {
            "identity": row.identity,
            "kind": row.kind,
            "source": str(row.path),
            "recipe": row.project.recipe_for(scope.unit).document(),
            "coordinates": [value if math.isfinite(value) else None for value in coordinates(row.trial)],
        }
        for row in active
    )
    write(
        {
            "kind": "search.stop",
            "stop_reason": stop_reason,
            "frontier": frontier,
            "telemetry": telemetry,
            "hints": hints,
        }
    )
    return SearchResult(
        best.path, best.trial, counters["measured"], steps, tuple(skips), stop_reason, frontier, telemetry, hints
    )
