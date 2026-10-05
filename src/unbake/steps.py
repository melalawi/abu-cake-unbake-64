"""Internal steps. Each runs only when the input it names changed, then records that input's key.

A step runs the steps it needs first (Step.needs), so asking for `types` also builds the map it reads.
Steps write project state, so only a writer (the cycle coordinator, publish, check, setup, recompute)
runs them. Read-only commands use whatever the last run produced.
"""

from __future__ import annotations

import json
import os
import sys
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from unbake import atomic as atomic_files
from unbake import effort
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
    value = _read(project)
    if value.pop(step, None) is not None:
        atomic_files.text(_path(project), json.dumps(value, indent=1, sort_keys=True) + "\n")


@dataclass(frozen=True)
class StepResult:
    step: str
    trigger: str
    ran: bool
    seconds: float
    spent: effort.Effort | None = None
    # Budgets this step (or, on a chain's last result, the chain) went over, each named by its budget key.
    findings: tuple[str, ...] = ()
    # Wall-time misses while other processes held the host busy: reported, never a failure.
    contended: tuple[str, ...] = ()

    def document(self) -> dict[str, Any]:
        return {
            "step": self.step,
            "trigger": self.trigger,
            "ran": self.ran,
            "seconds": round(self.seconds, 3),
            **(self.spent.document() if self.spent is not None else {}),
            "findings": list(self.findings),
            "contended": list(self.contended),
        }

    def line(self) -> str:
        text = f"{self.step}: unchanged ({self.trigger})"
        if self.ran and self.spent is not None:
            spent = self.spent
            text = (
                f"{self.step}: ran in {self.seconds:.1f} s, {spent.cpu:.1f} cpu-s ({spent.percent:.0f}%), "
                f"peak RSS main {spent.main_rss / effort.MB:.0f} MB, worker {spent.worker_rss / effort.MB:.0f} MB "
                f"({self.trigger})"
            )
        return "".join(
            [text, *(f"\n  OVER {finding}" for finding in self.findings), *(f"\n  {note}" for note in self.contended)]
        )


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
    from unbake.layout import map as layout_map
    from unbake.typemap import mapping, storage

    return key(
        "rom-facts",
        str(mapping.SCHEMA),
        json.dumps(storage.map_inputs(project), sort_keys=True),
        # layout.toml naming rows the split no longer holds is rendered again from the current split.
        json.dumps(layout_map.stale(project)),
    )


def _rom_facts(project: Project, host: Host) -> None:
    from unbake.layout import map as layout_map
    from unbake.typemap import mapping

    # Modules are ROM interval evidence: infer them before types and headers read the groups.
    layout_map.ensure(project)
    if (project.build / "map/facts.json").is_file():
        mapping.refresh_map(project, host)
    else:
        mapping.map_program(project, host)


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
    """Shape edits the ROM proves (alignment filler, cut tails) first, then one splat process per version,
    [setup].version_jobs of them at once. The step records the key of the split it extracted (after the edits)."""
    from concurrent.futures import ThreadPoolExecutor

    from unbake import extract
    from unbake.layout import shape_edits

    shape_edits.run(project, host)

    from unbake import cache
    from unbake.layout import split

    try:
        with ThreadPoolExecutor(max_workers=host.setup_version_jobs) as executor:
            list(executor.map(lambda version: extract.segments(project, host, version), project.versions))
    finally:
        # splat rewrites asm files in place, which no directory signature records.
        cache.forget([split.ASM_ROWS])


def _progress_key(project: Project, host: Host) -> str:
    from unbake.report import progress

    sources = sorted([*project.src.glob("*.c"), *project.src.glob("*.s")])
    return key("progress", str(progress.SCHEMA), project.root / "layout.toml", *sources)


def _progress(project: Project, host: Host) -> None:
    from unbake.report import progress

    progress.write(project, host)


def _trim_key(project: Project, host: Host) -> str:
    from unbake import cache

    total = sum(path.stat().st_size for path in cache.entries(project.cache))
    return "over" if total > host.cache_max_bytes else "under"


def _trim(project: Project, host: Host) -> None:
    from unbake import cache

    cache.trim(project.cache, host.cache_max_bytes, host.cache_trim_to_bytes)


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
    the next command reruns nothing. A key that still changes after one pass per step (and one more for
    types, which reads the headers it writes) is refused by name.
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
    """The step one ensure is running, so an interrupted or failed step is run again: its record is forgotten.

    Every completed step stands: it recorded the key of what it wrote, and the steps that publish layout.toml
    (extract's shape edits, merge-units) commit it together with the split and symbol files. Restoring an
    earlier layout.toml after such a step would name rows the split no longer has. A step that fails restores
    its own files (shape edits and merge-units keep a backup; the map step writes layout.toml in one atomic
    write). Kept per process on disk; a later command rolls back the journal of a process that died."""

    def __init__(self, project: Project, path: Path | None = None) -> None:
        self.project = project
        self.path = path or project.build / "steps.journal" / f"{os.getpid()}.json"
        self.state: dict[str, str | None] = {"running": None}
        if path is not None:
            self.state = json.loads(path.read_text())

    @classmethod
    def stale(cls, project: Project) -> list[Command]:
        directory = project.build / "steps.journal"
        found = []
        for path in sorted(directory.glob("*.json")) if directory.is_dir() else ():
            if not path.stem.isdigit():
                raise Held(
                    "steps",
                    f"steps.journal: {path} is not named for a process id; roll it back by moving it out of "
                    "the journal directory, then rerun",
                )
            pid = int(path.stem)
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                found.append(cls(project, path))
            except PermissionError:
                pass
        return found

    def running(self, name: str | None) -> None:
        """NAME starts (None: the step that was running finished and recorded its key)."""
        self.state["running"] = name
        atomic_files.write(self.path, json.dumps(self.state).encode())

    def rollback(self) -> None:
        name = self.state.get("running")
        if name is not None:
            forget(self.project, name)
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
    requested = set(names := list(names))
    steps = order(names)
    # A step whose recorded output went missing or changed runs first: it regenerates from its recorded
    # inputs (headers from the installed solution), and a later step must not read the damaged file.
    damaged = [name for name in steps if STEPS[name].outputs is not None and altered(project, name)]
    steps = damaged + [name for name in steps if name not in damaged]
    results: list[StepResult] = []
    chain = effort.mark()
    # One pass per step, and one more for a solve to read the headers it wrote.
    for attempt in range(len(steps) + 2):
        ran = []
        for name in steps:
            step = STEPS[name]
            started = time.monotonic()
            effort.window()
            spent = effort.mark()
            current = step.key(project, host)
            same = recorded(project, name) == current
            changed = altered(project, name) if same and step.outputs is not None else []
            if not (force and attempt == 0 and name in requested) and same and not changed:
                if attempt == 0:
                    results.append(StepResult(name, step.trigger, False, 0.0))
                continue
            trigger = f"an output is missing or changed: {', '.join(changed)}" if changed else step.trigger
            command.running(name)
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
                # These steps write their own inputs (extract applies shape edits to the split; rom-facts renders
                # layout.toml): the key after the run is the one the next command sees. Types records the key it
                # read, so a solve that changed the generated headers runs again on them (solver._types_key).
                step.key(project, host)
                if name in ("extract", "rom-facts", "resident", "headers", "buildfiles")
                else current,
                None if step.outputs is None else _digests(project, step.outputs(project)),
            )
            command.running(None)
            used = effort.since(spent)
            result = StepResult(
                name,
                trigger,
                True,
                time.monotonic() - started,
                used,
                tuple(effort.step_findings(name, used, host)),
            )
            _event({"event": "step.effort", **result.document()})
            results = [row for row in results if row.step != name or row.ran]
            results.append(result)
            ran.append(name)
            if report is not None:
                report(result)
        if not ran:
            return _checked(host, results, chain, force=force)
    raise Held(
        "steps", f"steps.{ran[0]}: input key changes on every run ({', '.join(ran)}); a step rewrites its inputs"
    )


def _event(document: dict[str, Any]) -> None:
    """A step's or chain's effort as one JSON line on stderr (stdout carries only the command's result)."""
    print(json.dumps(document, sort_keys=True), file=sys.stderr, flush=True)


def _checked(host: Host, results: list[StepResult], chain: effort.Mark, *, force: bool) -> list[StepResult]:
    """The chain's cost against the host's [budgets]: findings go on its last result and in its event."""
    used = effort.since(chain)
    kind = "recompute" if force else "changed" if any(row.ran for row in results) else "unchanged"
    findings, notes = effort.chain_findings(kind, used, host)
    _event({"event": "steps.effort", "kind": kind, **used.document(), "findings": findings, "contended": notes})
    if (findings or notes) and results:
        last = results[-1]
        results[-1] = replace(last, findings=(*last.findings, *findings), contended=tuple(notes))
    return results


def recompute(
    project: Project, host: Host, names: Iterable[str], report: Callable[[StepResult], object] | None = None
) -> list[StepResult]:
    return ensure(project, host, names, force=True, report=report)
