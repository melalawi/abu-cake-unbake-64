"""Measured source search with a content cache and bounded beam."""

from __future__ import annotations

import hashlib
import json
import math
import re
import subprocess
import time
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from unbake import atomic as atomic_files
from unbake import tui
from unbake.compilers.ranking import measured_candidate_rank
from unbake.config import Held, Host, Project
from unbake.decomp import explain
from unbake.process import read_text
from unbake.work.compare import Compared, measure


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
    allocation: explain.Allocation
    focus_lines: tuple[int, ...]
    deadline: float


@dataclass(frozen=True)
class SearchResult:
    source: Path
    trial: Compared
    score: int
    fuzzy: float
    trials: int
    steps: Path


@dataclass(frozen=True)
class _Candidate:
    source: str
    path: Path
    trial: Compared
    score: int
    fuzzy: float

    @property
    def rank(self) -> tuple[bool, int, int, float]:
        return measured_candidate_rank(self.trial.compares, self.fuzzy)


def _positive(policy: Host, name: str) -> int:
    value = getattr(policy, name, None)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise Held("search", f"policy.{name}: positive integer required")
    return value


def preprocess(project: Project, policy: Host, source: Path, version: str, deadline: float) -> str:
    """Use the selected unit's build preprocessor, includes and VERSION flags."""
    from unbake.compilers import drivers

    command = drivers.preprocess_command(project, str(policy.cpp), version, source.stem, source, non_matching=True)
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise Held("search", "context.deadline: preprocessing budget exhausted")
    try:
        result = subprocess.run(command, cwd=project.root, capture_output=True, text=True, timeout=remaining)
    except subprocess.TimeoutExpired as error:
        raise Held("search", "context.deadline: preprocessing budget exhausted") from error
    except OSError as error:
        raise Held("search", f"preprocessor {command[0]}: {error}") from error
    if result.returncode:
        raise Held("search", f"preprocessor {command[0]} exited {result.returncode}: {result.stderr.strip()}")
    expanded = re.sub(r"^\s*#\s*(?:line\s+)?\d+[^\n]*", "", result.stdout, flags=re.M)
    # Preprocessors discard comments; retain explicit source evidence in mutations.
    comments = re.findall(r'/\*.*?\*/|//[^\n]*|"(?:\\.|[^"\\])*"', source.read_text(), re.S)
    markers = [comment for comment in comments if comment.startswith("/") and "FAKEMATCH:" in comment]
    return "".join(comment + "\n" for comment in markers if comment not in expanded) + expanded


def _focus_lines(allocation: explain.Allocation, original: str, expanded: str) -> tuple[int, ...]:
    candidates = {number for difference in allocation.differences for number in difference.candidates}
    lines = {line for pseudo in allocation.pseudos if pseudo.number in candidates for line in pseudo.source_lines}
    source_lines = original.splitlines()
    expressions = {source_lines[line - 1].strip() for line in lines if 1 <= line <= len(source_lines)}
    return tuple(index for index, text in enumerate(expanded.splitlines(), 1) if text.strip() in expressions)


def run(
    project: Project, policy: Host, source: Path, generators: Iterable[Generator], out: Path, budget_seconds: float
) -> SearchResult:
    from unbake.search.permute import Permuter

    width = _positive(policy, "search_beam")
    stall_limit = _positive(policy, "stall_trials")
    if (
        isinstance(budget_seconds, bool)
        or not isinstance(budget_seconds, (int, float))
        or not math.isfinite(budget_seconds)
        or budget_seconds <= 0
    ):
        raise Held("search", "budget_seconds: positive finite number required")
    generators = list(generators)
    if not generators:
        raise Held("search", "generators: missing value")
    for generator in generators:
        if not callable(getattr(generator, "propose", None)):
            raise Held("search", f"generator {generator}: propose missing")
    source = Path(source).resolve()
    text = read_text(source, "search")
    out.mkdir(parents=True, exist_ok=True)
    steps = out / "steps.jsonl"
    cache: dict[str, _Candidate | None] = {}
    evaluated = 0
    deadline = time.monotonic() + budget_seconds
    mutation_seconds = 0.0
    prepared: dict[tuple[str, str], tuple[str, explain.Allocation, tuple[int, ...]]] = {}

    def evaluate(
        content: str, method: str, mutation: Mutation, version: str | None = None, incumbent: _Candidate | None = None
    ) -> _Candidate | None:
        nonlocal evaluated, mutation_seconds, deadline
        digest = hashlib.sha256(content.encode()).hexdigest()
        cached = digest in cache
        error = None
        measured_score: int | None = None
        confirmed = False
        if not cached:
            started = time.monotonic()
            evaluation_deadline = deadline
            directory = out / digest
            directory.mkdir(exist_ok=True)
            path = directory / source.name
            atomic_files.text(path, content, encoding="utf-8")
            try:
                result = (
                    measure(project, policy, path)
                    if version is None
                    else measure(project, policy, path, versions=(version,))
                )
            except Held as failure:
                error = failure.reason
                cache[digest] = None
            else:
                if not result.compares:
                    raise Held("search", "trial.compares: missing VERSION")
                measured_score = min(c.identical for c in result.compares.values())
                cache[digest] = None
                if version is None or (
                    incumbent is not None
                    and measured_candidate_rank(result.compares)
                    < measured_candidate_rank({version: incumbent.trial.compares[version]})
                ):
                    if version is not None:
                        confirmation_started = time.monotonic()
                        try:
                            result = measure(project, policy, path)
                        except Held as failure:
                            error = failure.reason
                        else:
                            confirmed = True
                        deadline += time.monotonic() - confirmation_started
                    else:
                        confirmed = True
                    if confirmed:
                        fuzzy = result.best_percent
                        cache[digest] = _Candidate(
                            content, path, result, min(c.identical for c in result.compares.values()), fuzzy
                        )
            evaluated += 1
            if version is not None:
                mutation_seconds = max(mutation_seconds, time.monotonic() - started - (deadline - evaluation_deadline))
        candidate = cache[digest]
        row = {
            "generator": method,
            "mutation": mutation.description,
            "kind": mutation.kind,
            "score": candidate.score if candidate else measured_score,
            "versions": list(candidate.trial.compares) if candidate else ([version] if version else []),
            "confirmed": confirmed or (cached and candidate is not None),
            "fuzzy": candidate.fuzzy if candidate else None,
            "source_sha256": digest,
            "cached": cached,
            "refusal": error,
        }
        with atomic_files.stream(steps, "a", encoding="utf-8") as stream:
            stream.write(json.dumps(row, sort_keys=True, allow_nan=False) + "\n")
        return candidate

    initial = evaluate(text, "initial", Mutation("initial", "starting source", text))
    if initial is None:
        raise Held("search", f"source {source}: initial trial failed; see {steps}")
    best = initial
    # Baseline and initial compiler context are setup, outside the mutation budget.
    representative = min(best.trial.compares, key=lambda v: (best.trial.compares[v].identical, v))
    for generator in generators:
        version = generator.version if isinstance(generator, Permuter) else representative
        key = best.trial.source_sha256, version
        expanded = preprocess(project, policy, best.path, version, time.monotonic() + budget_seconds)
        allocation = explain.allocation(project, policy, best.path, version)
        prepared[key] = expanded, allocation, _focus_lines(allocation, best.source, expanded)
    deadline = time.monotonic() + budget_seconds
    beam = [best]
    stalls = 0
    while time.monotonic() < deadline and not best.trial.identical_everywhere:
        pool = {item.trial.source_sha256: item for item in beam}
        fresh = False
        previous = best.rank
        for parent in beam:
            generator_deadline = deadline - mutation_seconds
            if time.monotonic() >= generator_deadline:
                break
            parent_digest = parent.trial.source_sha256
            for generator in generators:
                generator_deadline = deadline - mutation_seconds
                if time.monotonic() >= generator_deadline:
                    break
                version = generator.version if isinstance(generator, Permuter) else representative
                key = parent_digest, version
                if key not in prepared:
                    expanded = preprocess(project, policy, parent.path, version, generator_deadline)
                    allocation = explain.allocation(project, policy, parent.path, version)
                    prepared[key] = expanded, allocation, _focus_lines(allocation, parent.source, expanded)
                expanded, allocation, focus_lines = prepared[key]
                context = Context(project, policy, out, parent.path, allocation, focus_lines, generator_deadline)
                method = getattr(generator, "name", type(generator).__name__)
                proposals = iter(generator.propose(expanded, parent.trial, context))
                while time.monotonic() < deadline - mutation_seconds:
                    try:
                        mutation = next(proposals)
                    except StopIteration:
                        break
                    if time.monotonic() >= deadline:
                        break
                    if not isinstance(mutation, Mutation) or not isinstance(mutation.source, str):
                        raise Held("search", f"generator {method}.mutation: Mutation with source text required")
                    digest = hashlib.sha256(mutation.source.encode()).hexdigest()
                    fresh |= digest not in cache
                    candidate = evaluate(mutation.source, method, mutation, version, best)
                    if candidate:
                        pool[digest] = candidate
                        if candidate.rank < best.rank:
                            best = candidate
                        if best.trial.identical_everywhere:
                            break
                if best.trial.identical_everywhere or time.monotonic() >= deadline - mutation_seconds:
                    break
            if best.trial.identical_everywhere or time.monotonic() >= deadline - mutation_seconds:
                break
        beam = sorted(pool.values(), key=lambda item: item.rank)[:width]
        stalls = 0 if best.rank < previous else stalls + 1
        if stalls >= stall_limit:
            beam = [best]
            stalls = 0
        if not fresh:
            if beam != [best]:
                beam = [best]
            else:
                break
    mutations = evaluated - 1
    if mutations == 0 and time.monotonic() < deadline - mutation_seconds:
        # The methods proposed nothing for this source with budget to spare: the start is the best they have.
        tui.line("no mutation proposed; the starting source is the best")
    elif mutations == 0:
        if not any(isinstance(generator, Permuter) and generator.ran for generator in generators):
            raise Held("search", f"zero mutations evaluated; increase budget or select another method; see {steps}")
        tui.line("external permuter ran; no improving candidates emitted")
    if best.trial.identical_everywhere:
        tui.verdict("cracked", f"IDENTICAL {best.trial.function}: {best.path}")
    return SearchResult(best.path, best.trial, best.score, best.fuzzy, evaluated, steps)
