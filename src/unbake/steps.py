"""Internal steps. Each runs only when the input it names changed, then records that input's key.

A step runs the steps it needs first (Step.needs), so asking for `types` also builds the map it reads.
Steps write project state, so only a writer (the cycle coordinator, publish, check, setup, recompute)
runs them. Read-only commands use whatever the last run produced.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field, replace
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Any

from unbake import atomic as atomic_files
from unbake import cache as retention
from unbake import effort, tui
from unbake.cache import key
from unbake.config import Held, Host, Project
from unbake.inputs import DependencySet
from unbake.process import Action, capture
from unbake.process import named as cause_named

if TYPE_CHECKING:
    from unbake.decomp.checks import Findings, SourceFinding


@dataclass(frozen=True)
class PrepareRequest:
    operation: str
    sources: tuple[Path, ...]
    proposed: dict[Path, str] = field(default_factory=dict)
    required_steps: tuple[str, ...] = ()
    resume_command: str | None = None
    project_scope: bool = False
    rules: str = "unmarked"


@dataclass(frozen=True)
class Prepared:
    request: PrepareRequest
    findings: Findings
    source_inventory: tuple[Path, ...]

    @property
    def blocked(self) -> tuple[SourceFinding, ...]:
        return self.findings.rows if self.request.rules == "all" else self.findings.unmarked

    def document(self) -> dict[str, Any]:
        return {
            "phase": "preflight",
            "blocked_before_build": bool(self.blocked),
            "built": False,
            "reused": self.findings.source_scans == 0,
            "findings": [row.document() for row in self.blocked],
            "dependency_hashes": self.findings.dependency_hashes,
            "resume_command": self.request.resume_command,
            "work": {"source_scans": self.findings.source_scans, "step_runs": 0, "make_invocations": 0},
        }

    def refuse(self) -> None:
        if not self.blocked:
            return
        first = self.blocked[0]
        finding = first.finding
        place = f"{first.path}:{finding.line}"
        provenance = "; present before edit" if first.predates_edit else ""
        resume = self.request.resume_command
        action = f"stop: fix {finding.rule} at {place}" + (f", then run {resume}" if resume else "")
        raise Held(
            cause_named(
                "check.source_rules",
                f"check.source_rules: {place} {finding.rule}{provenance}; build not started",
                owner="steps",
                stage="preflight",
                action=Action("stop", reason=action),
            ),
            data=self.document(),
        )

    def assert_current(self, project: Project) -> None:
        from unbake import inputs
        from unbake.decomp import checks

        expected = self.findings.dependency_hashes
        if checks.recipe() != expected["recipe:source-rules"]:
            raise Held(
                cause_named(
                    "prepare.changed",
                    "prepare.changed: source-rule recipe changed since preparation",
                    owner="steps",
                    stage="preflight",
                )
            )
        if self.request.project_scope and tuple(sorted(project.src.glob("*.c"))) != self.source_inventory:
            raise Held(
                cause_named(
                    "prepare.changed",
                    "prepare.changed: source inventory changed since preparation",
                    owner="steps",
                    stage="preflight",
                )
            )
        for name, digest in expected.items():
            if name.startswith("recipe:"):
                continue
            path = project.root / name
            current = (
                inputs.digest(path, algorithm="sha256", reuse=retention.configured()) if path.is_file() else "missing"
            )
            if current != digest:
                raise Held(
                    cause_named(
                        "prepare.changed",
                        f"prepare.changed: {name}: changed since preparation",
                        owner="steps",
                        stage="preflight",
                    )
                )


def prepare(project: Project, host: Host, request: PrepareRequest) -> Prepared:
    """Cheap request/rule checks precede freshness work and every proposed file installation."""
    from unbake.cache import Cache
    from unbake.decomp import checks

    if request.rules not in ("all", "unmarked"):
        raise Held(
            cause_named("prepare.rules", "prepare.rules: expected all or unmarked", owner="steps", stage="preflight")
        )
    inventory = tuple(sorted(project.src.glob("*.c")))
    paths = tuple(sorted(set(inventory if request.project_scope else request.sources) | set(request.proposed)))
    for path in paths:
        if not path.resolve().is_relative_to(project.root.resolve()):
            raise Held(
                cause_named(
                    "prepare.source", f"prepare.source: {path}: outside project", owner="steps", stage="preflight"
                )
            )
        if path not in request.proposed and not path.is_file():
            raise Held(
                cause_named(
                    "prepare.source", f"prepare.source: {path}: missing source", owner="steps", stage="preflight"
                )
            )
    from unbake import inputs
    from unbake.work.attempts import RetryScope

    dependencies = inputs.DependencySet(
        tuple(
            inputs.file_pin(path, root=project.root, root_id="project", reuse=retention.configured()) for path in paths
        ),
        {
            "proposed": {
                str(p.relative_to(project.root)): inputs.bytes_digest(v.encode(), algorithm="sha256")
                for p, v in request.proposed.items()
            },
            "rules": request.rules,
        },
        {"source-rules": checks.recipe()},
    )
    with RetryScope(
        project,
        "prepare",
        request.operation,
        {"sources": [str(p.relative_to(project.root)) for p in request.sources], "rules": request.rules},
        dependencies,
    ) as scope:
        result = Prepared(
            request,
            checks.findings(project, paths, Cache(project.build / "cache"), proposed=request.proposed),
            inventory,
        )
        result.refuse()
        result.assert_current(project)
        if request.required_steps:
            ensure(project, host, request.required_steps)
        scope.value = result.document()
        return result


def recorded(project: Project, step: str) -> str | None:
    from unbake.work.attempts import ledger

    entry = ledger(project).step(step)
    return entry["key"] if entry else None


def record(project: Project, step: str, content_key: str, outputs: dict[str, str] | None = None) -> None:
    from unbake.work.attempts import ledger

    ledger(project).record_step(step, content_key, outputs or {})


def _digests(project: Project, paths: Iterable[Path]) -> dict[str, str]:
    from unbake import inputs

    return {
        str(path.relative_to(project.root)): inputs.digest(path, algorithm="sha256", reuse=retention.configured())
        for path in sorted(paths)
        if path.is_file()
    }


def acknowledge_outputs(project: Project, step: str, paths: Iterable[Path]) -> None:
    """Update only recorded outputs a successful proven operation itself wrote.

    Keep the input key: a later solve still refreshes declarations from the new
    source. Other outputs retain their digests so hand edits remain detectable.
    """
    from unbake.work.attempts import ledger

    entry = ledger(project).step(step)
    if entry is None:
        return
    outputs = dict(entry.get("outputs", {}))
    from unbake import inputs

    for path in paths:
        name = str(path.relative_to(project.root))
        if name in outputs and path.is_file():
            outputs[name] = inputs.digest(path, algorithm="sha256", reuse=retention.configured())
    if outputs != entry.get("outputs", {}):
        entry["outputs"] = outputs
        record(project, step, entry["key"], outputs)


def altered(project: Project, step: str) -> list[str]:
    """The recorded outputs of STEP that are now missing or hold other bytes."""
    from unbake import inputs
    from unbake.work.attempts import ledger

    entry = ledger(project).step(step)
    changed = []
    for name, digest in sorted((entry or {}).get("outputs", {}).items()):
        path = project.root / name
        if not path.is_file() or inputs.digest(path, algorithm="sha256", reuse=retention.configured()) != digest:
            changed.append(name)
    return changed


def forget(project: Project, step: str) -> None:
    from unbake.inputs import DependencySet
    from unbake.work.attempts import ledger

    ledger(project).note("step", step, {"invalidated": True}, dependencies=DependencySet((), {}, {}))


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
    # What the step changed, by kind (the types step: the answers that differ from the last solve).
    changes: dict[str, Any] = field(default_factory=dict)

    def document(self) -> dict[str, Any]:
        return {
            "step": self.step,
            "trigger": self.trigger,
            "ran": self.ran,
            "seconds": round(self.seconds, 3),
            **(self.spent.document() if self.spent is not None else {}),
            "findings": list(self.findings),
            "contended": list(self.contended),
            **({"changes": self.changes} if self.changes else {}),
        }


@dataclass(frozen=True)
class Step:
    name: str
    # What a person reads while the step runs.
    label: str
    trigger: str
    key: Callable[[Project, Host], str]
    # Returns what it changed by kind (the types step), else None.
    run: Callable[[Project, Host], dict[str, Any] | None]
    needs: tuple[str, ...] = ()
    # The files the step writes; a recorded one that goes missing or changes makes the step run again.
    outputs: Callable[[Project], Iterable[Path]] | None = None
    # Readiness is checked before cache reuse, independently of the recorded input key.
    ready: Callable[[Project], bool] | None = None


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


def _types_ready(project: Project) -> bool:
    from unbake.typemap import types_db

    return types_db.compatible(types_db.path(project))


def _types(project: Project, host: Host) -> dict[str, Any]:
    from unbake.layout import header_step
    from unbake.typemap import solver, types_db

    # The solve preprocesses published sources, which include generated headers: a tree missing any is first
    # regenerated from the recorded solution.
    if types_db.compatible(types_db.path(project)) and header_step.missing(project):
        header_step.run(project, host)
    result = solver.solve(project, host)
    return {**result["changes"], "post_input_key": result["post_input_key"]}


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
        with ThreadPoolExecutor(max_workers=min(host.setup_version_jobs, host.cores)) as executor:
            list(executor.map(lambda version: extract.segments(project, host, version), project.versions))
    finally:
        # splat rewrites asm files in place, which no directory signature records.
        cache.forget([split.ASM_ROWS])


def _progress_key(project: Project, host: Host) -> str:
    from unbake.report import progress

    return progress.input_key(project)


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


def _compiler_headers_key(project: Project, host: Host) -> str:
    from unbake.compilers import headers

    return headers.input_key(project)


def _compiler_headers(project: Project, host: Host) -> None:
    from unbake.compilers import headers

    headers.run(project)


def _compiler_headers_outputs(project: Project) -> Iterable[Path]:
    from unbake.compilers import headers

    return headers.outputs(project)


STEPS: dict[str, Step] = {
    step.name: step
    for step in (
        Step(
            "compiler-headers",
            "Provisioning public compiler headers",
            "compiler providers or imported types changed",
            _compiler_headers_key,
            _compiler_headers,
            outputs=_compiler_headers_outputs,
        ),
        Step(
            "extract", "Splitting the ROMs into code and data", "ROM sha1 or split rows changed", _extract_key, _extract
        ),
        Step(
            "rom-facts",
            "Reading what each function's assembly does",
            "interval or symbol rows changed (infers default modules)",
            _rom_facts_key,
            _rom_facts,
            ("extract",),
        ),
        Step(
            "types",
            "Working out C types for functions, globals and structs",
            "a published source's facts changed",
            _types_key,
            _types,
            ("rom-facts",),
            ready=_types_ready,
        ),
        Step(
            "headers",
            "Updating the shared headers in include/",
            "layout.toml or the type solution changed",
            _headers_key,
            _headers,
            ("types",),
            _headers_outputs,
        ),
        Step(
            "buildfiles",
            "Regenerating the Makefile",
            "layout, units, compilers or build flags changed",
            _buildfiles_key,
            _buildfiles,
        ),
        Step("progress", "Updating the progress numbers", "a land or a boundary edit", _progress_key, _progress),
        Step(
            "resident",
            "Removing data blocks that published C now owns",
            "a published source changed (resident constant blocks are deleted)",
            _resident_key,
            _resident,
        ),
        Step(
            "merge-units",
            "Joining finished neighbouring files",
            "a land made a run of matched members",
            _merge_units_key,
            _merge_units,
        ),
        Step("trim-cache", "Trimming the cache", "the cache passed [cache].max_bytes", _trim_key, _trim),
    )
}
NAMES: tuple[str, ...] = tuple(STEPS)


def order(names: Iterable[str]) -> list[str]:
    """The named steps with every step they need placed before them, each once, in request order."""
    result: list[str] = []

    def visit(name: str) -> None:
        if name not in STEPS:
            raise Held(
                cause_named(
                    f"steps.{name}",
                    f"steps.{name}: unknown step; expected one of {', '.join(NAMES)}",
                    owner="steps",
                    stage="steps",
                )
            )
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
        from unbake.work.attempts import command_ledger

        with command_ledger(project):
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
                    cause_named(
                        "steps.journal",
                        (
                            f"steps.journal: {path} is not named for a process id; roll it back by "
                            f"moving it out of the journal directory, then rerun"
                        ),
                        owner="steps",
                        stage="steps",
                    )
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
    unready = set()
    for name in steps:
        readiness = STEPS[name].ready
        if readiness is not None and not readiness(project):
            unready.add(name)
    damaged = [
        name
        for name in steps
        if STEPS[name].outputs is not None and altered(project, name) and not (set(order([name])) & unready)
    ]
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
            from unbake.work.attempts import RetryScope

            trial = RetryScope(project, "step.request", name, {}, operation_dependencies(project, host, name))
            trial.__enter__()
            trial.scope.__exit__(None, None, None)
            current = _reading_current(project, partial(step.key, project, host))
            same = recorded(project, name) == current
            changed = altered(project, name) if same and step.outputs is not None else []
            ready = step.ready is None or step.ready(project)
            if not (force and attempt == 0 and name in requested) and same and not changed and ready:
                if attempt == 0:
                    results.append(StepResult(name, step.trigger, False, 0.0))
                continue
            trigger = (
                "the installed output is stale or missing"
                if not ready
                else f"an output is missing or changed: {', '.join(changed)}"
                if changed
                else step.trigger
            )
            command.running(name)
            changes: dict[str, Any] = {}
            from unbake.work.attempts import RetryScope

            try:
                with (
                    RetryScope(project, "step.request", name, {}, operation_dependencies(project, host, name)),
                    tui.task(step.label) as shown,
                ):
                    changes = _reading_current(project, partial(step.run, project, host)) or {}
                    if name == "types":
                        count = sum(
                            kind["count"] for kind in changes.values() if isinstance(kind, dict) and "count" in kind
                        )
                        shown.note = f"; changed {count} answers" if count else "; nothing changed"
            except Held:
                raise
            except Exception as error:
                raise Held(
                    capture(
                        error,
                        cause=cause_named(
                            f"steps.{name}", f"{type(error).__name__}: {error}", owner="steps", stage="steps"
                        ),
                    )
                ) from error
            recorded_key = current
            if name in ("extract", "rom-facts", "resident", "headers", "buildfiles"):
                recorded_key = step.key(project, host)
            elif name == "types":
                recorded_key = changes.pop("post_input_key", current)
            record(
                project,
                name,
                recorded_key,
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
                changes=changes,
            )
            results = [row for row in results if row.step != name or row.ran]
            results.append(result)
            ran.append(name)
            if report is not None:
                report(result)
        if not ran:
            return _checked(host, results, chain, force=force)
    raise Held(
        cause_named(
            f"steps.{ran[0]}",
            f"steps.{ran[0]}: input key changes on every run ({', '.join(ran)}); a step rewrites its inputs",
            owner="steps",
            stage="steps",
        )
    )


def _reading_current(project: Project, call: Callable[[], Any]) -> Any:
    """An unchanged disappearance is a refusal, rather than a blind replay."""
    return call()


def _checked(host: Host, results: list[StepResult], chain: effort.Mark, *, force: bool) -> list[StepResult]:
    """The chain's cost against the host's [budgets]: findings go on its last result and in its event."""
    used = effort.since(chain)
    kind = "recompute" if force else "changed" if any(row.ran for row in results) else "unchanged"
    findings, notes = effort.chain_findings(kind, used, host)
    if (findings or notes) and results:
        last = results[-1]
        results[-1] = replace(last, findings=(*last.findings, *findings), contended=tuple(notes))
    return results


def recompute(
    project: Project, host: Host, names: Iterable[str], report: Callable[[StepResult], object] | None = None
) -> list[StepResult]:
    names = list(names)
    if "types" in order(names):
        # A forced types step solves again, whatever the last solution's inputs were.

        forget(project, "types")
    return ensure(project, host, names, force=True, report=report)


def operation_dependencies(project: Project, host: Host, step: str) -> DependencySet:
    from unbake import inputs

    paths = {
        project.root / "config.toml",
        project.root / "layout.toml",
        *(p for root in project.include for p in root.rglob("*.h")),
        *(p for v in project.versions for p in (project.version(v).split, project.version(v).symbols)),
    }
    if step not in ("headers",):
        paths.update(project.src.rglob("*.c"))
    root = Path(__file__).parent
    modules = {
        "types": (
            "typemap/solver.py",
            "typemap/declarations.py",
            "cdecl.py",
            "typemap/facts.py",
            "typemap/closure.py",
            "typemap/database.py",
            "typemap/regeneration.py",
            "pool.py",
            "cache.py",
            "process.py",
            "compilers/drivers.py",
            "project/headers.py",
        ),
        "headers": ("layout/header_step.py", "layout/header_loss.py", "project/headers.py", "cdecl.py"),
    }.get(step, ("steps.py",))
    values: dict[str, Any] = {
        "step": step,
        "versions": list(project.versions),
        "memory_worker_bytes": host.memory_worker_bytes,
    }
    if step in ("types", "headers") and isinstance(host, Host):
        values["cache_memory_bytes"] = host.cache_memory_bytes
        values["native"] = {
            field: inputs.digest(Path(getattr(host, field)), algorithm="sha256", reuse=retention.configured())
            for field in ("cpp", "m2c")
        }
        values["header_inventory"] = sorted(str(p.relative_to(project.root)) for p in paths if p.suffix == ".h")
    return inputs.DependencySet(
        tuple(
            inputs.file_pin(p, root=project.root, root_id="project", reuse=retention.configured())
            for p in sorted(paths)
        ),
        values,
        {name: inputs.digest(root / name, algorithm="sha256", reuse=retention.configured()) for name in modules},
    )
