"""Internal steps. Each runs only when the input it names changed, then records that input's key.

A step runs the steps it needs first (Step.needs), so asking for `types` also builds the map it reads.
Steps write project state, so only a writer (the cycle coordinator, publish, check, setup, recompute)
runs them. Read-only commands use whatever the last run produced.
"""

from __future__ import annotations

import functools
import json
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from unbake import atomic as atomic_files
from unbake.cache import key
from unbake.config import Held, Host, Project


def _path(project: Project) -> Path:
    return project.build / "steps.json"


def _read(project: Project) -> dict[str, str]:
    path = _path(project)
    if not path.is_file():
        return {}
    try:
        value = json.loads(path.read_text())
    except (OSError, ValueError) as error:
        raise Held("steps", f"steps.json: {path}: {error}") from error
    if not isinstance(value, dict):
        raise Held("steps", f"steps.json: {path}: expected object")
    return {name: text for name, text in value.items() if isinstance(text, str)}


def recorded(project: Project, step: str) -> str | None:
    return _read(project).get(step)


def record(project: Project, step: str, content_key: str) -> None:
    value = _read(project)
    value[step] = content_key
    path = _path(project)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_files.text(path, json.dumps(value, indent=1, sort_keys=True) + "\n")


def forget(project: Project, step: str) -> None:
    value = _read(project)
    if value.pop(step, None) is not None:
        atomic_files.text(_path(project), json.dumps(value, indent=1, sort_keys=True) + "\n")


@functools.cache
def tool_fingerprint() -> str:
    """Digest of the installed tool's code; a tool change reruns every step once."""
    root = Path(__file__).resolve().parent
    return key(*sorted(root.rglob("*.py")))


@dataclass(frozen=True)
class StepResult:
    step: str
    trigger: str
    ran: bool
    seconds: float

    def document(self) -> dict[str, Any]:
        return {"step": self.step, "trigger": self.trigger, "ran": self.ran, "seconds": round(self.seconds, 3)}

    def line(self) -> str:
        state = f"ran in {self.seconds:.1f} s" if self.ran else "unchanged"
        return f"{self.step}: {state} ({self.trigger})"


@dataclass(frozen=True)
class Step:
    name: str
    trigger: str
    key: Callable[[Project, Host], str]
    run: Callable[[Project, Host], None]
    needs: tuple[str, ...] = ()


def _rom_facts_key(project: Project, host: Host) -> str:
    from unbake.typemap import storage

    return key(tool_fingerprint(), json.dumps(storage.map_inputs(project), sort_keys=True))


def _rom_facts(project: Project, host: Host) -> None:
    from unbake.typemap import mapping

    if (project.build / "map/facts.json").is_file():
        mapping.refresh_map(project)
    else:
        mapping.map_program(project)


def _types_key(project: Project, host: Host) -> str:
    from unbake.typemap import solver

    return solver.input_key(project, host)


def _types(project: Project, host: Host) -> None:
    from unbake.typemap import solver

    solver.solve(project, host)


def _headers_key(project: Project, host: Host) -> str:
    from unbake.layout import header_step

    return header_step.input_key(project)


def _headers(project: Project, host: Host) -> None:
    from unbake.layout import header_step

    header_step.run(project, host)


def _buildfiles_key(project: Project, host: Host) -> str:
    from unbake import buildfiles

    return buildfiles.input_key(project, host)


def _buildfiles(project: Project, host: Host) -> None:
    from unbake import buildfiles

    buildfiles.write(project, host)


def _extract_key(project: Project, host: Host) -> str:
    from unbake import extract

    return extract.input_key(project, host)


def _extract(project: Project, host: Host) -> None:
    """One splat process per version, [setup].version_jobs of them at once."""
    from concurrent.futures import ThreadPoolExecutor

    from unbake import extract

    with ThreadPoolExecutor(max_workers=host.setup_version_jobs) as executor:
        list(executor.map(lambda version: extract.segments(project, host, version), project.versions))


def _progress_key(project: Project, host: Host) -> str:
    return key(tool_fingerprint(), project.root / "layout.toml", *sorted(project.src.glob("*.c")))


def _progress(project: Project, host: Host) -> None:
    from unbake.report import progress

    progress.write(project, host)


def _trim_key(project: Project, host: Host) -> str:
    from unbake import cache

    total = sum(path.stat().st_size for path in cache.entries(host.cache_root))
    return "over" if total > host.cache_max_bytes else "under"


def _trim(project: Project, host: Host) -> None:
    from unbake import cache

    cache.trim(host.cache_root, host.cache_max_bytes, host.cache_trim_to_bytes)


def _resident_key(project: Project, host: Host) -> str:
    from unbake.layout import resident

    return resident.input_key(project)


def _resident(project: Project, host: Host) -> None:
    from unbake.layout import resident

    resident.run(project, host)


def _merge_units_key(project: Project, host: Host) -> str:
    from unbake.layout import merge_units

    return merge_units.input_key(project)


def _merge_units(project: Project, host: Host) -> None:
    from unbake.layout import merge_units

    merge_units.run(project, host)


STEPS: dict[str, Step] = {
    step.name: step
    for step in (
        Step("extract", "ROM sha1 or split rows changed", _extract_key, _extract),
        Step("rom-facts", "interval or symbol rows changed", _rom_facts_key, _rom_facts, ("extract",)),
        Step("types", "a published source's facts changed", _types_key, _types, ("rom-facts",)),
        Step("headers", "layout.toml or the type solution changed", _headers_key, _headers, ("types",)),
        Step("buildfiles", "layout, units, compilers or build flags changed", _buildfiles_key, _buildfiles),
        Step("progress", "a land or a boundary edit", _progress_key, _progress),
        Step("resident", "a published source changed (resident constant blocks are deleted)", _resident_key, _resident),
        Step("merge-units", "a land made a run of matched members", _merge_units_key, _merge_units),
        Step("trim-cache", "the cache passed [cache].max_bytes", _trim_key, _trim),
    )
}
NAMES: tuple[str, ...] = tuple(STEPS)


def order(names: Iterable[str]) -> list[str]:
    """The named steps with every step they need placed before them, each once, in request order."""
    result: list[str] = []

    def visit(name: str) -> None:
        if name not in STEPS:
            raise Held("steps", f"steps.{name}: unknown step; expected one of {', '.join(NAMES)}")
        if name in result:
            return
        for needed in STEPS[name].needs:
            visit(needed)
        result.append(name)

    for name in names:
        visit(name)
    return result


def ensure(
    project: Project,
    host: Host,
    names: Iterable[str],
    *,
    force: bool = False,
    report: Callable[[StepResult], object] | None = None,
) -> list[StepResult]:
    """Run each named step (and the steps it needs) whose input key changed; forcing reruns only the named.

    report hears each step that ran as soon as it finishes, so a long chain is not silent."""
    requested = set(names := list(names))
    results = []
    for name in order(names):
        step = STEPS[name]
        started = time.monotonic()
        current = step.key(project, host)
        if not (force and name in requested) and recorded(project, name) == current:
            results.append(StepResult(name, step.trigger, False, 0.0))
            continue
        step.run(project, host)
        record(project, name, step.key(project, host) if name in ("resident", "headers", "buildfiles") else current)
        results.append(StepResult(name, step.trigger, True, time.monotonic() - started))
        if report is not None:
            report(results[-1])
    return results


def recompute(project: Project, host: Host, names: Iterable[str]) -> list[StepResult]:
    return ensure(project, host, names, force=True)
