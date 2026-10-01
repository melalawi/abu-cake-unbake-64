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

from unbake.decomp import explain, trial
from unbake.decomp.trial_compile import read_text, scratch_directory
from unbake.project import makefile
from unbake.project.config import Held, Policy, Project


@dataclass(frozen=True)
class Mutation:
    kind: str
    description: str
    source: str


class Generator(Protocol):
    def propose(self, source: str, trial: trial.Trial, ctx: Context) -> Iterable[Mutation]: ...


@dataclass(frozen=True)
class Context:
    project: Project
    policy: Policy
    out: Path
    source: Path
    allocation: explain.Allocation
    focus_lines: tuple[int, ...]
    deadline: float


@dataclass(frozen=True)
class SearchResult:
    source: Path
    trial: trial.Trial
    score: int
    fuzzy: float
    trials: int
    steps: Path


@dataclass(frozen=True)
class _Candidate:
    source: str
    path: Path
    trial: trial.Trial
    score: int
    fuzzy: float

    @property
    def rank(self) -> tuple[int, float]:
        return self.score, self.fuzzy


def _positive(policy: Policy, name: str) -> int:
    value = getattr(policy, name, None)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise Held("search", f"policy.{name}: positive integer required")
    return value


def _retain(
    project: Project, policy: Policy, source: Path, scratch: Path, result: trial.Trial, generations: dict[str, Path]
) -> float:
    from unbake.cli.decomp import store_trial

    store_trial(project, policy, source, scratch, result, generations)
    from unbake.decomp.drafts import Store

    rows = Store(policy, project).rows(result.function)
    matching = [row for row in rows if row["sha256"] == result.source_sha256]
    if not matching:
        raise Held("search", f"draft store {result.source_sha256}: missing row")
    scores = matching[-1]["score"]
    if not scores or set(scores) != set(result.compares):
        raise Held("search", "fuzzy scores: missing VERSION")
    return min(scores.values())


def preprocess(project: Project, policy: Policy, source: Path, version: str, deadline: float) -> str:
    """Use the selected unit's build preprocessor, includes and VERSION flags."""
    compiler = project.compiler_for(source)
    flags = list(makefile.flags(project, version, source))
    if compiler.kind == "sn64":
        from unbake.project_tools.sn64_cc import partition_flags

        try:
            options, _ = partition_flags(flags)
        except ValueError as error:
            raise Held("search", f"compiler.cflags: {error}") from error
        recipe = makefile.recipe(project)
        cpp = makefile.host_executable(policy, recipe.cpp or "", "cpp")
        command = [cpp, *recipe.cppflags, *options, "-DNON_MATCHING=1", str(source)]
    else:
        if not compiler.cc:
            raise Held("search", "compiler.cc: missing value")
        command = [str(compiler.cc), *(flag for flag in flags if flag != "-c"), "-DNON_MATCHING=1", "-E", str(source)]
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
    project: Project, policy: Policy, source: Path, generators: Iterable[Generator], out: Path, budget_seconds: float
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
    out = scratch_directory(project, out, "search")
    steps = out / "steps.jsonl"
    cache: dict[str, _Candidate | None] = {}
    evaluated = 0
    deadline = time.monotonic() + budget_seconds
    confirmation_seconds = 0.0
    prepared: dict[tuple[str, str], tuple[str, explain.Allocation, tuple[int, ...]]] = {}

    def evaluate(content: str, method: str, mutation: Mutation) -> _Candidate | None:
        nonlocal evaluated, confirmation_seconds
        digest = hashlib.sha256(content.encode()).hexdigest()
        cached = digest in cache
        error = None
        if not cached:
            started = time.monotonic()
            directory = out / digest
            directory.mkdir(exist_ok=True)
            path = directory / source.name
            path.write_text(content, encoding="utf-8")
            scratch = directory / "trial"
            generations = {v: project.build_link(v).resolve() for v in project.versions}
            try:
                result = trial.try_draft(project, policy, path, scratch)
            except Held as failure:
                error = failure.reason
                cache[digest] = None
            else:
                if not result.compares:
                    raise Held("search", "trial.compares: missing VERSION")
                fuzzy = _retain(project, policy, path, scratch, result, generations)
                cache[digest] = _Candidate(
                    content, path, result, min(c.identical for c in result.compares.values()), fuzzy
                )
            evaluated += 1
            confirmation_seconds = max(confirmation_seconds, time.monotonic() - started)
        candidate = cache[digest]
        row = {
            "generator": method,
            "mutation": mutation.description,
            "kind": mutation.kind,
            "score": candidate.score if candidate else None,
            "fuzzy": candidate.fuzzy if candidate else None,
            "source_sha256": digest,
            "cached": cached,
            "refusal": error,
        }
        with steps.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(row, sort_keys=True, allow_nan=False) + "\n")
        return candidate

    best = evaluate(text, "initial", Mutation("initial", "starting source", text))
    if best is None:
        raise Held("search", f"source {source}: initial trial failed; see {steps}")
    beam = [best]
    stalls = 0
    while time.monotonic() < deadline and not best.trial.identical_everywhere:
        pool = {item.trial.source_sha256: item for item in beam}
        fresh = False
        previous = best.rank
        for parent in beam:
            generator_deadline = deadline - confirmation_seconds
            if time.monotonic() >= generator_deadline:
                break
            parent_digest = parent.trial.source_sha256
            for generator in generators:
                generator_deadline = deadline - confirmation_seconds
                if time.monotonic() >= generator_deadline:
                    break
                version = (
                    generator.version
                    if isinstance(generator, Permuter)
                    else min(parent.trial.compares, key=lambda v: (parent.trial.compares[v].identical, v))
                )
                key = parent_digest, version
                if key not in prepared:
                    expanded = preprocess(project, policy, parent.path, version, generator_deadline)
                    allocation = explain.allocation(project, policy, parent.path, version)
                    prepared[key] = expanded, allocation, _focus_lines(allocation, parent.source, expanded)
                expanded, allocation, focus_lines = prepared[key]
                context = Context(project, policy, out, parent.path, allocation, focus_lines, generator_deadline)
                method = getattr(generator, "name", type(generator).__name__)
                proposals = iter(generator.propose(expanded, parent.trial, context))
                while time.monotonic() < deadline - confirmation_seconds:
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
                    candidate = evaluate(mutation.source, method, mutation)
                    if candidate:
                        pool[digest] = candidate
                        if candidate.rank > best.rank:
                            best = candidate
                        if best.trial.identical_everywhere:
                            break
                if best.trial.identical_everywhere or time.monotonic() >= deadline - confirmation_seconds:
                    break
            if best.trial.identical_everywhere or time.monotonic() >= deadline - confirmation_seconds:
                break
        beam = sorted(pool.values(), key=lambda item: item.rank, reverse=True)[:width]
        stalls = 0 if best.rank > previous else stalls + 1
        if stalls >= stall_limit:
            beam = [best]
            stalls = 0
        if not fresh:
            if beam != [best]:
                beam = [best]
            else:
                break
    if best.trial.identical_everywhere:
        print(f"IDENTICAL {best.trial.function}: {best.path}")
    else:
        print(f"best {best.score} words; next_command: {best.trial.next_command}")
    return SearchResult(best.path, best.trial, best.score, best.fuzzy, evaluated, steps)
