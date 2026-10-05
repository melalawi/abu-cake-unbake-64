"""Internal steps. Each runs only when the input it names changed, then records that input's key.

A step runs the steps it needs first (Step.needs), so asking for `types` also builds the map it reads.
Steps write project state, so only a writer (the cycle coordinator, publish, check, setup, recompute)
runs them. Read-only commands use whatever the last run produced.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import threading
import time
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from unbake import atomic as atomic_files
from unbake.cache import key
from unbake.config import Held, Host, Project


def _path(project: Project) -> Path:
    return project.build / "steps.json"


def _read(project: Project) -> dict[str, dict[str, Any]]:
    """Each step's record: the input key it ran for and the digest of each output it left (relative path)."""
    path = _path(project)
    if not path.is_file():
        return {}
    try:
        value = json.loads(path.read_text())
    except (OSError, ValueError) as error:
        raise Held("steps", f"steps.json: {path}: {error}") from error
    if not isinstance(value, dict):
        raise Held("steps", f"steps.json: {path}: expected object")
    return {
        name: entry for name, entry in value.items() if isinstance(entry, dict) and isinstance(entry.get("key"), str)
    }


def recorded(project: Project, step: str) -> str | None:
    entry = _read(project).get(step)
    return None if entry is None else entry["key"]


def record(project: Project, step: str, content_key: str, outputs: dict[str, str] | None = None) -> None:
    with _updating(project):
        value = _read(project)
        value[step] = {"key": content_key, "outputs": outputs or {}}
        atomic_files.text(_path(project), json.dumps(value, indent=1, sort_keys=True) + "\n")


def _digests(project: Project, paths: Iterable[Path]) -> dict[str, str]:
    from unbake import inputs

    return {str(path.relative_to(project.root)): inputs.digest(path) for path in sorted(paths) if path.is_file()}


def altered(project: Project, step: str) -> list[str]:
    """The recorded outputs of STEP that are now missing or hold other bytes."""
    from unbake import inputs

    entry = _read(project).get(step)
    changed = []
    for name, digest in sorted((entry or {}).get("outputs", {}).items()):
        path = project.root / name
        if not path.is_file() or inputs.digest(path) != digest:
            changed.append(name)
    return changed


def forget(project: Project, step: str) -> None:
    with _updating(project):
        value = _read(project)
        if value.pop(step, None) is not None:
            atomic_files.text(_path(project), json.dumps(value, indent=1, sort_keys=True) + "\n")


@contextmanager
def _updating(project: Project) -> Iterator[None]:
    """One read-modify-write of steps.json at a time: a cycle records steps from two threads."""
    from unbake import lock

    with lock.exclusive(project.build / "steps.json.lock"):
        yield


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
    # The files the step writes; a recorded one that goes missing or changes makes the step run again.
    outputs: Callable[[Project], Iterable[Path]] | None = None


def _rom_facts_key(project: Project, host: Host) -> str:
    from unbake.typemap import mapping, storage

    return key("rom-facts", str(mapping.SCHEMA), json.dumps(storage.map_inputs(project), sort_keys=True))


def _rom_facts(project: Project, host: Host) -> None:
    from unbake.layout import map as layout_map
    from unbake.typemap import mapping

    # Modules are ROM interval evidence: infer them before types and headers read the groups.
    layout_map.ensure(project)
    if (project.build / "map/facts.json").is_file():
        mapping.refresh_map(project)
    else:
        mapping.map_program(project)


def _types_key(project: Project, host: Host) -> str:
    from unbake.typemap import solver

    return solver.input_key(project, host)


def _types(project: Project, host: Host) -> None:
    from unbake.layout import header_step
    from unbake.typemap import solver

    # The solve preprocesses published sources, which include generated headers: a tree missing any is first
    # regenerated from the recorded solution.
    if header_step.missing(project):
        header_step.run(project, host)
    solver.solve(project, host)


def _headers_key(project: Project, host: Host) -> str:
    from unbake.layout import header_step

    return header_step.input_key(project)


def _headers(project: Project, host: Host) -> None:
    from unbake.layout import header_step

    header_step.run(project, host)


def _headers_outputs(project: Project) -> Iterable[Path]:
    from unbake.layout import index

    return [index.path(project), *index.listed(project)]


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
    from unbake.report import progress

    return key("progress", str(progress.SCHEMA), project.root / "layout.toml", *sorted(project.src.glob("*.c")))


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
        Step(
            "rom-facts",
            "interval or symbol rows changed (infers default modules)",
            _rom_facts_key,
            _rom_facts,
            ("extract",),
        ),
        Step("types", "a published source's facts changed", _types_key, _types, ("rom-facts",)),
        Step(
            "headers",
            "layout.toml or the type solution changed",
            _headers_key,
            _headers,
            ("types",),
            _headers_outputs,
        ),
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

    A step can write another's input (merge-units rewrites the split extract and rom-facts read), so the
    steps pass again until none ran: the command leaves every recorded key equal to its current key, and
    the next command reruns nothing. A key that still changes after one pass per step is refused by name.
    report hears each step that ran as soon as it finishes, so a long chain is not silent."""
    from unbake import journal
    from unbake.layout import header_step

    # A command killed mid-write is rolled back before any step reads the tree.
    journal.recover(header_step.journal_path(project))
    for stale in Command.stale(project):
        stale.rollback()
    command = Command(project)
    try:
        results = _ensure(project, host, names, force=force, report=report, command=command)
    except BaseException:
        command.rollback()
        raise
    command.close()
    return results


class Command:
    """What one ensure changed, so a failed command publishes none of it: the steps it recorded (forgotten on
    rollback) and layout.toml's bytes before its first rewrite (restored only while the file still holds what
    this command wrote, so a concurrent merge-units write is never undone). Kept per process and thread on disk;
    a later command rolls back the journal of a process that died."""

    def __init__(self, project: Project, path: Path | None = None) -> None:
        self.project = project
        self.path = path or project.build / "steps.journal" / f"{os.getpid()}-{threading.get_ident()}.json"
        self.state: dict[str, object] = {"ran": [], "before": None, "after": None}
        if path is not None:
            self.state = json.loads(path.read_text())

    @classmethod
    def stale(cls, project: Project) -> list[Command]:
        directory = project.build / "steps.journal"
        found = []
        for path in sorted(directory.glob("*.json")) if directory.is_dir() else ():
            pid = int(path.stem.split("-", 1)[0])
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                found.append(cls(project, path))
            except PermissionError:
                pass
        return found

    def _save(self) -> None:
        atomic_files.write(self.path, json.dumps(self.state).encode())

    def ran(self, name: str, before: bytes | None, after: bytes | None) -> None:
        ran = self.state["ran"]
        assert isinstance(ran, list)
        ran.append(name)
        if before != after and self.state["after"] is None:
            self.state["before"] = None if before is None else base64.b64encode(before).decode()
        if before != after:
            self.state["after"] = None if after is None else hashlib.sha256(after).hexdigest()
        self._save()

    def rollback(self) -> None:
        from unbake import lock

        layout = self.project.root / "layout.toml"
        with lock.publishing(self.project.root):
            for name in self.state["ran"]:  # type: ignore[attr-defined]
                forget(self.project, name)
            after, before = self.state["after"], self.state["before"]
            if after is not None and layout.is_file() and hashlib.sha256(layout.read_bytes()).hexdigest() == after:
                if before is None:
                    layout.unlink()
                else:
                    atomic_files.write(layout, base64.b64decode(str(before)))
        self.close()

    def close(self) -> None:
        self.path.unlink(missing_ok=True)


def _ensure(
    project: Project,
    host: Host,
    names: Iterable[str],
    *,
    force: bool,
    report: Callable[[StepResult], object] | None,
    command: Command,
) -> list[StepResult]:
    layout = project.root / "layout.toml"
    requested = set(names := list(names))
    steps = order(names)
    # A step whose recorded output went missing or changed runs first: it regenerates from its recorded
    # inputs (headers from the installed solution), and a later step must not read the damaged file.
    damaged = [name for name in steps if STEPS[name].outputs is not None and altered(project, name)]
    steps = damaged + [name for name in steps if name not in damaged]
    results: list[StepResult] = []
    for attempt in range(len(steps) + 1):
        ran = []
        for name in steps:
            step = STEPS[name]
            started = time.monotonic()
            current = step.key(project, host)
            same = recorded(project, name) == current
            changed = altered(project, name) if same and step.outputs is not None else []
            if not (force and attempt == 0 and name in requested) and same and not changed:
                if attempt == 0:
                    results.append(StepResult(name, step.trigger, False, 0.0))
                continue
            trigger = f"an output is missing or changed: {', '.join(changed)}" if changed else step.trigger
            before = layout.read_bytes() if layout.is_file() else None
            try:
                step.run(project, host)
            except Held:
                raise
            except Exception as error:
                where = f" reading {error.filename}" if isinstance(error, OSError) and error.filename else ""
                raise Held("steps", f"steps.{name}: {type(error).__name__}{where}: {error}") from error
            record(
                project,
                name,
                # These steps write their own inputs (types publishes the headers its source facts preprocess):
                # the key after the run is the one the next command sees.
                step.key(project, host) if name in ("types", "resident", "headers", "buildfiles") else current,
                None if step.outputs is None else _digests(project, step.outputs(project)),
            )
            command.ran(name, before, layout.read_bytes() if layout.is_file() else None)
            result = StepResult(name, trigger, True, time.monotonic() - started)
            results = [row for row in results if row.step != name or row.ran]
            results.append(result)
            ran.append(name)
            if report is not None:
                report(result)
        if not ran:
            return results
    raise Held(
        "steps", f"steps.{ran[0]}: input key changes on every run ({', '.join(ran)}); a step rewrites its inputs"
    )


def recompute(project: Project, host: Host, names: Iterable[str]) -> list[StepResult]:
    return ensure(project, host, names, force=True)
