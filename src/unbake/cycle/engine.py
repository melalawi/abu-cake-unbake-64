"""The cycle coordinator: pick, draft and compare in worker processes, land each exact function.

One thread owns all shared state, writes the project tree and waits on one queue. Messages come from worker
futures (draft and compare results), the file watcher (saved drafts) and, on a terminal, the
key reader. Workers only read the tree. An exact function lands once the pool has drained: then the lands and
the steps they change run on this thread, and every task that read the tree before is done again. Nothing
polls: the only timed wait is the stop condition's own deadline.
"""

from __future__ import annotations

import hashlib
import json
import queue
import threading
import time
from collections.abc import Callable
from concurrent.futures import Future, wait
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, TextIO

from unbake import atomic as atomic_files
from unbake import tui
from unbake.cli.output import Result
from unbake.config import Held, Host, Project
from unbake.cycle import ladder, rank
from unbake.cycle.events import Emitter
from unbake.process import fault as cause_fault

STAGES = (
    "queued",
    "drafting",
    "comparing",
    "searching",
    "needs creative",
    "waiting for edit",
    "landing",
    "landed",
    "held",
    "failed",
)


@dataclass
class Row:
    function: str
    bytes: int
    versions: tuple[str, ...]
    carryover: bool
    stage: str = "queued"
    best_percent: float | None = None
    tries: int = 0
    started: float = field(default_factory=time.monotonic)
    diagnostic: str = ""
    file: str = ""
    sha256: str = ""
    held: bool = False
    commit: str = ""
    # The bytes of the last generated draft: a draft still holding them is drafted again after the steps change.
    drafted_sha: str = ""
    # Where the mechanical search ladder stands; a fresh compare of the tree or a human edit starts it over.
    ladder: ladder.Ladder = field(default_factory=ladder.Ladder)


# ---- worker tasks (module level so the fork server can import them) ----


def _draft_task(spec: tuple[Path, Host, str, bool]) -> dict[str, Any]:
    from unbake import config
    from unbake.work import draft

    root, host, function, replace = spec
    started = time.monotonic()
    try:
        made = draft.draft(config.load(root), host, function, replace=replace)
    except Held as error:
        return {
            "ok": False,
            "key": error.key,
            "diagnostic": error.reason,
            "fault": cause_fault(error),
            "seconds": time.monotonic() - started,
        }
    return {"ok": True, "file": str(made.file), "seconds": time.monotonic() - started}


SKIPPED = "the method proposed no mutation for this function"

FIRST = ("VERSION ", "first divergence: ", "constant: ")


def first_difference(lines: list[str]) -> str:
    """The first line that says why a version is not exact: a compile or link refusal, else the first divergence."""
    return next((line for line in lines if line.startswith(FIRST)), "")


def _compare_task(spec: tuple[Path, Host, str]) -> dict[str, Any]:
    from unbake import config
    from unbake.work import compare

    root, host, file = spec
    started = time.monotonic()
    try:
        measured = compare.compare(config.load(root), host, Path(file))
    except Held as error:
        return {
            "ok": False,
            "key": error.key,
            "diagnostic": error.reason,
            "fault": cause_fault(error),
            "seconds": time.monotonic() - started,
        }
    return {
        "ok": not measured.faults,
        "sha256": measured.source_sha256,
        "per_version": {
            v: {
                "percent": round(c.match_percent, 6),
                "exact": c.exact,
                "first": first_difference(c.lines),
                **({"fault": measured.faults[v]} if v in measured.faults else {}),
            }
            for v, c in measured.compares.items()
        },
        "best_percent": None if measured.faults else measured.best_percent,
        "exact": measured.exact,
        "diagnostic": next((first_difference(measured.compares[v].lines) for v in measured.faults), "")
        or next((first_difference(c.lines) for c in measured.compares.values() if not c.exact), "")
        or next((f"rule broken: {line}" for line in measured.rule_lines), ""),
        "seconds": time.monotonic() - started,
    }


def _search_task(spec: tuple[Path, Host, str, str]) -> dict[str, Any]:
    from unbake import config
    from unbake.work import search

    root, host, file, method = spec
    started = time.monotonic()
    try:
        found = search.search(config.load(root), host, Path(file), method, host.cycle_search_seconds)
    except Held as error:
        return {
            "ok": False,
            "key": error.key,
            "diagnostic": error.reason,
            "fault": cause_fault(error),
            "seconds": time.monotonic() - started,
        }
    return {
        "ok": True,
        "best_file": str(found.best_file),
        "mutations": found.mutations,
        "words": found.words,
        "seconds": time.monotonic() - started,
    }


def _recheck_task(spec: tuple[Path, Host, str]) -> dict[str, Any]:
    """A landed function measured again as the unit it lives in (merge-units may have moved it): the unit is built
    with the current headers and must equal the ROM row in every holding version, and break no source rule."""
    from unbake import config, runner
    from unbake.decomp import checks
    from unbake.layout import split
    from unbake.work import compare

    root, host, function = spec
    project = config.load(root)
    try:
        for version in split.holding_versions(project, function):
            row = compare.row_of(project, function, version)
            data = runner.build_unit(project, host, Path(row.path).name, version)
            if data != split.words(project, row):
                return {
                    "exact": False,
                    "best_percent": None,
                    "diagnostic": f"{function} no longer builds identical in {version} (unit {row.path})",
                }
            broken = checks.unmarked(project.src / f"{row.path}.c")
            if broken:
                return {
                    "exact": False,
                    "best_percent": None,
                    "diagnostic": f"rule broken: {checks.plain(broken[0])}",
                }
    except Held as error:
        return {"exact": False, "best_percent": None, "diagnostic": error.reason, "fault": cause_fault(error)}
    return {"exact": True, "best_percent": 100.0, "diagnostic": ""}


# ---- read-only entry points ----


def state_path(project: Project) -> Path:
    return project.build / "cycle" / "state.json"


def status(project: Project) -> dict[str, Any]:
    path = state_path(project)
    if not path.is_file():
        return {"running": False, "rows": [], "note": "no cycle has run in this project"}
    return dict(json.loads(path.read_text()))


def ranked(project: Project, host: Host) -> list[rank.Candidate]:
    from unbake.work import plan

    return plan.ranked(project, host)


def emit_row(stream: TextIO, row: rank.Candidate) -> None:
    stream.write(json.dumps(asdict(row), sort_keys=True) + "\n")
    stream.flush()


def interactive() -> bool:
    return tui.interactive()


def narrate(record: dict[str, Any]) -> None:
    """One plain sentence per event a person follows (the cycle's listener for both the board and the log)."""
    function = record.get("function", "")
    match record["event"]:
        case "fn.queued":
            tui.line(f"{function} ({record['bytes']} bytes, {', '.join(record['versions'])}): queued")
        case "fn.draft.done":
            tui.line(
                f"{function}: first draft compiles"
                if record["ok"]
                else f"{function}: draft failed: {record.get('diagnostic', '')}"
            )
        case "fn.compare.done":
            percent = record["best_percent"]
            tui.line(
                f"{function}: {percent}% of bytes match"
                if percent is not None
                else f"{function}: compare refused: {record.get('diagnostic', '')}"
            )
        case "fn.search.start":
            tui.line(f"{function}: trying {record['method']} changes")
        case "fn.search.done" if "words" in record:
            tui.line(
                f"  {function}: tried {record['mutations']} variants in {record['seconds']:g}s; "
                f"best leaves {record['words']} words different"
            )
        case "fn.exact":
            tui.verdict("cracked", f"{function}: byte-identical in every version")
        case "fn.creative":
            tui.verdict(
                "creative",
                f"{function}: best {record['best_percent']}%, mechanical tries ran out; notes in {record['trouble']}",
            )
        case "fn.landed":
            tui.line(f"{function}: landed ({record['bytes']} bytes)")
        case "fn.committed":
            tui.line(f"{function}: committed {record['commit'][:7]}")
        case "fn.held" | "fn.failed":
            tui.line(f"{function}: stopped: {record['reason']}")
            tui.line(f"  Next: {record['next']}")
        case "cycle.end":
            tui.line(
                f"Cycle done: {len(record['landed'])} landed ({record['landed_bytes']} bytes), "
                f"{len(record['held'])} stopped, {len(record['carryovers'])} carried over"
            )


# ---- the run ----


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


BUSY = frozenset({"drafting", "comparing", "searching", "landing"})


class Stop:
    """idle:N counts only time when no function is drafting, comparing or landing; after:N is wall time."""

    def __init__(self, condition: str | None, interactive: bool = False) -> None:
        # Without a person to edit files, all-landed ends once no work is left in flight (see reached).
        self.interactive = interactive
        self.condition = condition or "all-landed"
        self.started = time.monotonic()
        self.activity = self.started
        self.busy = False

    def note_activity(self) -> None:
        self.activity = time.monotonic()

    def timeout(self, rows: dict[str, Row]) -> float | None:
        """Seconds until the condition is reached; None while work is running or for all-landed."""
        kind, _, value = self.condition.partition(":")
        if kind == "idle":
            if any(row.stage in BUSY for row in rows.values()):
                self.busy = True
                return None
            if self.busy:
                # Idle time starts when the last draft, compare or land finishes.
                self.busy = False
                self.note_activity()
            return max(0.0, self.activity + int(value) - time.monotonic())
        if kind == "after":
            return max(0.0, self.started + int(value) * 60 - time.monotonic())
        return None

    def reached(self, rows: dict[str, Row]) -> bool:
        if self.condition == "all-landed":
            if self.interactive:
                return all(row.stage in ("landed", "held", "failed") for row in rows.values())
            # Nothing drafting, comparing, searching, landing or queued: every function is landed, stopped, or
            # waiting for a person (refused land, needs creative). No one can edit a headless run's files.
            return not any(row.stage in BUSY or row.stage == "queued" for row in rows.values())
        timeout = self.timeout(rows)
        return timeout is not None and timeout <= 0


def choose(project: Project, host: Host, pick: int | None, functions: tuple[str, ...]) -> list[rank.Candidate]:
    from unbake.work import plan

    if functions:
        # Named functions skip the [cycle] size window, which only narrows --pick and the picker.
        by_name = {row.function: row for row in plan.candidates(project, host)}
        missing = [name for name in functions if name not in by_name]
        if missing:
            reasons = "; ".join(f"{name}: {plan.refusal(project, name)}" for name in missing)
            raise Held("cycle", f"cycle.functions: not candidates: {reasons}")
        return [by_name[name] for name in functions]
    order = ranked(project, host)
    if pick is not None:
        return order[:pick]
    from unbake.tui import pick as picker

    return picker.pick(order)


# The tree a draft reads: brought current on this thread before the first draft (choose reads only the split,
# the map and layout.toml, so the picks come first), and again after every land, while nothing is in flight.
PICK_STEPS = ("resident", "rom-facts")
DRAFT_STEPS = ("types", "headers", "buildfiles")
LAND_STEPS = ("merge-units", "resident", *DRAFT_STEPS)


# A row in one of these stages last read the tree before the steps changed it (a held-back task is in them too).
REDONE = frozenset({"drafting", "comparing", "searching", "needs creative", "waiting for edit"})


@dataclass(frozen=True)
class Task:
    kind: str  # "draft", "compare" or "search"
    argument: str  # the function to draft, or the file to compare or search
    replace: bool = False
    method: str = ""  # the search method


def run(
    project: Project,
    host: Host,
    *,
    pick: int | None,
    functions: tuple[str, ...],
    stop: str | None,
    events: TextIO,
    next_words: Callable[..., str],
) -> Result:
    """One writer: this thread. Steps and lands write the tree only while no draft or compare is in flight, so a
    worker task reads one complete tree, and every task that read the tree before a write is done again after it."""
    from unbake import land, pool, steps

    before = land.dirty(project)
    started = steps.ensure(project, host, PICK_STEPS)
    picked = choose(project, host, pick, functions)
    if not picked:
        raise Held("cycle", "cycle.pick: nothing to work on (no candidates in the size window)")
    started += steps.ensure(project, host, DRAFT_STEPS)
    tui.line(f"Cycle on {len(picked)} functions: {', '.join(row.function for row in picked)}")
    emitter = Emitter(events)
    rows = {c.function: Row(c.function, c.bytes, c.versions, c.carryover, best_percent=c.best_percent) for c in picked}
    inbox: queue.Queue[tuple[str, Any]] = queue.Queue()
    stopper = Stop(stop, interactive())
    board = None
    if interactive():
        from unbake.tui import board as board_view

        board = board_view.Board(project, rows, inbox)
        emitter.listeners.append(board.on_event)
    emitter.listeners.append(narrate)

    def save_state(_: dict[str, Any] | None = None) -> None:
        state = {"running": True, "rows": [asdict(row) for row in rows.values()]}
        atomic_files.text(state_path(project), json.dumps(state, sort_keys=True, default=list) + "\n")

    emitter.listeners.append(save_state)

    def report(done: Any) -> None:
        emitter.emit(
            "step.run",
            step=done.step,
            trigger=done.trigger,
            seconds=round(done.seconds, 3),
            **(done.spent.document() if done.spent is not None else {}),
            **({"changes": done.changes} if done.changes else {}),
            **({"findings": list(done.findings)} if done.findings else {}),
        )

    for done in started:
        if done.ran:
            report(done)
    emitter.emit(
        "cycle.start",
        project=str(project.root),
        versions=list(project.versions),
        functions=list(rows),
        workers=pool.admitted(
            host.workers, host.memory_total_bytes, host.memory_parent_bytes, host.memory_worker_bytes
        ),
        cores=host.cores,
        memory_total_bytes=host.memory_total_bytes,
        cache_root=str(project.cache),
        stop=stopper.condition,
    )
    landed: list[str] = []
    # The task each row has in the pool; its result is read only while it is still the row's task.
    inflight: dict[str, tuple[Future[dict[str, Any]], Task]] = {}
    # Tasks held back while a land waits for the pool to drain; they start after the land's steps.
    deferred: dict[str, Task] = {}
    # Exact rows waiting for the pool to drain, in arrival order.
    exact: list[str] = []
    retried: set[tuple[str, str]] = set()
    watcher_stop = threading.Event()
    from unbake.cycle import watcher

    watching = threading.Thread(
        target=watcher.watch, args=(project.work, host.cycle_debounce_ms, watcher_stop, inbox), daemon=True
    )
    exit_code = 0
    steps_error = ""
    # Lands proved against headers their land's steps then replaced are measured again; a miss is never ignored.
    unchecked: list[str] = []
    regressed: list[dict[str, Any]] = []

    def recheck() -> None:
        results = pool.run(host, _recheck_task, [(project.root, host, function) for function in unchecked])
        for function, result in zip(unchecked, results, strict=True):
            emitter.emit(
                "fn.recheck",
                function=function,
                exact=bool(result["exact"]),
                best_percent=result["best_percent"],
                **({"diagnostic": result["diagnostic"]} if result["diagnostic"] else {}),
                **({"fault": result["fault"]} if "fault" in result else {}),
            )
            if not result["exact"]:
                regressed.append({"function": function, "diagnostic": result["diagnostic"]})
        unchecked.clear()

    def bring_current(names: tuple[str, ...]) -> list[str]:
        """Run steps on this thread; a held step ends the cycle (drafts must never read a half-current tree)."""
        nonlocal steps_error
        try:
            return [done.step for done in steps.ensure(project, host, names, report=report) if done.ran]
        except Held as error:
            steps_error = error.reason
            emitter.emit("steps.held", key=error.key, reason=error.reason, fault=cause_fault(error))
            return []

    watching.start()

    with pool.Pool.from_host(host) as workers, pool.sharing(workers):

        def submit(function: str, task: Task) -> None:
            """One task per row at a time: while the row's task runs, or a land waits for the pool to drain, the
            newest task is held back and starts after it."""
            if exact or function in inflight:
                deferred[function] = task
                return
            deferred.pop(function, None)
            if task.kind == "draft":
                future = workers.submit(_draft_task, (project.root, host, task.argument, task.replace))
            elif task.kind == "search":
                future = workers.submit(_search_task, (project.root, host, task.argument, task.method))
            else:
                future = workers.submit(_compare_task, (project.root, host, task.argument))
            inflight[function] = (future, task)
            future.add_done_callback(lambda done: inbox.put((task.kind, (function, done))))

        def start(row: Row, *, redraft: bool = False) -> None:
            if row.held:
                return
            row.ladder = ladder.Ladder()
            file = project.work / row.function / f"{row.function}.c"
            if redraft:
                row.stage, row.sha256 = "drafting", ""
                emitter.emit("fn.draft.start", function=row.function)
                submit(row.function, Task("draft", row.function, replace=True))
            elif file.is_file():
                row.file = str(file)
                row.sha256 = _sha(file)
                row.stage = "comparing"
                emitter.emit("fn.compare.start", function=row.function, sha256=row.sha256)
                submit(row.function, Task("compare", str(file)))
            else:
                row.stage = "drafting"
                emitter.emit("fn.draft.start", function=row.function)
                submit(row.function, Task("draft", row.function))

        def redo(row: Row) -> None:
            """A row whose last draft or compare read the tree before the steps changed it: an untouched draft is
            drafted again, an edited one compared again, a refused draft drafted again."""
            file = Path(row.file) if row.file else project.work / row.function / f"{row.function}.c"
            if file.is_file() and row.drafted_sha and _sha(file) == row.drafted_sha:
                start(row, redraft=True)
            else:
                start(row)

        def finish_land(row: Row) -> None:
            message = land.subject(project, row.function)
            started = time.monotonic()
            try:
                commit = land.land(project, host, Path(row.file))
            except Held as error:
                again = (row.function, row.sha256) not in retried
                retried.add((row.function, row.sha256))
                emitter.emit(
                    "fn.land_failed",
                    function=row.function,
                    versions=list(row.versions),
                    diagnostic=error.reason,
                    fault=cause_fault(error),
                    returned_to_worker=again,
                )
                row.diagnostic = error.reason
                if again:
                    start(row)
                else:
                    row.stage = "waiting for edit"
                return
            row.stage, row.commit = "landed", commit
            landed.append(row.function)
            unchecked.append(row.function)
            emitter.emit(
                "fn.landed",
                function=row.function,
                bytes=row.bytes,
                versions=list(row.versions),
                seconds=round(time.monotonic() - started, 3),
                retried=(row.function, row.sha256) in retried,
            )
            emitter.emit("fn.committed", function=row.function, commit=commit, message=message)

        def search_next(row: Row) -> None:
            """The next method of the ladder, else the function needs a creative edit."""
            method = row.ladder.next_method()
            if method is None:
                creative(row)
                return
            row.ladder.method = method
            row.stage = "searching"
            emitter.emit("fn.search.start", function=row.function, method=method)
            submit(row.function, Task("search", row.file, method=method))

        def creative(row: Row) -> None:
            file = Path(row.file)
            row.stage = "needs creative"
            row.best_percent = row.ladder.best
            trouble = ladder.write_trouble(project, host, row.function, file, row.ladder, row.diagnostic)
            emitter.emit(
                "fn.creative",
                function=row.function,
                best_percent=row.ladder.best,
                methods={**row.ladder.tried, **{name: f"skipped: {why}" for name, why in row.ladder.skipped.items()}},
                trouble=str(trouble),
            )

        def restore(row: Row) -> None:
            """A method that gained nothing leaves the best text in the file."""
            file = Path(row.file)
            atomic_files.text(file, ladder.snapshot_path(file).read_text(), encoding="utf-8")
            row.sha256 = _sha(file)

        def climb(row: Row, percent: float) -> None:
            """A compare that is not exact: score the method whose text it measured, else start the ladder."""
            file = Path(row.file)
            current = row.ladder
            method, current.method = current.method, ""
            if method:
                gained = current.gained(percent)
                current.tried[method] = percent
                if not gained:
                    restore(row)
                    creative(row)
                    return
            current.best = percent
            atomic_files.text(ladder.snapshot_path(file), file.read_text(), encoding="utf-8")
            search_next(row)

        def searched(row: Row, result: dict[str, Any]) -> None:
            method, current = row.ladder.method, row.ladder
            emitter.emit(
                "fn.search.done",
                function=row.function,
                method=method,
                ok=result["ok"],
                seconds=round(result["seconds"], 3),
                **({} if result["ok"] else {"diagnostic": result["diagnostic"]}),
                **({"fault": result["fault"]} if "fault" in result else {}),
                **({"diagnostic": SKIPPED} if result["ok"] and not result["mutations"] else {}),
                **({"mutations": result["mutations"], "words": result["words"]} if result["ok"] else {}),
            )
            if not result["ok"]:
                # A method that errors is a tool gap, never a plateau: the row holds with the method's reason.
                current.method = ""
                row.stage, row.diagnostic = "held", f"search.{method}: {result['diagnostic']}"
                emitter.emit(
                    "fn.held",
                    function=row.function,
                    key=result["key"],
                    reason=row.diagnostic,
                    **({"fault": result["fault"]} if "fault" in result else {}),
                    next=next_words("search-variants", row.file, "--method", method),
                )
                return
            if not result["mutations"]:
                # Nothing to mutate (no pseudo register to move, no statement to reorder): the method does not
                # apply to this function, which says nothing about the ones after it.
                current.skipped[method] = SKIPPED
                current.method = ""
                search_next(row)
                return
            file, best = Path(row.file), Path(result["best_file"])
            if best.read_bytes() == file.read_bytes():
                current.tried[method] = current.best
                current.method = ""
                creative(row)
                return
            # The method's best text is compared like any edit; the watcher's event for this write matches the
            # recorded digest and is ignored.
            atomic_files.copyfile(best, file)
            row.sha256 = _sha(file)
            row.stage = "comparing"
            emitter.emit("fn.compare.start", function=row.function, sha256=row.sha256)
            submit(row.function, Task("compare", row.file))

        def land_drained(*, resume: bool) -> None:
            """The pool is empty: land every exact row, bring the tree current once, redo what read the old tree,
            then start the held-back tasks (unless the cycle is ending)."""
            while exact:
                function = exact[0]
                finish_land(rows[function])
                exact.pop(0)
            ran = bring_current(LAND_STEPS)
            if ran:
                recheck()
                # The steps' work left memos in the workers; drafts start in clean ones.
                workers._fresh()
            if not resume or steps_error:
                deferred.clear()
                return
            held_back, stale = (
                dict(deferred),
                [
                    row
                    for row in rows.values()
                    if ran
                    and not row.held
                    and (row.stage in REDONE or (row.stage == "held" and {"types", "headers"} & set(ran)))
                ],
            )
            deferred.clear()
            for row in stale:
                held_back.pop(row.function, None)
                redo(row)
            for function, task in held_back.items():
                submit(function, task)

        def drain() -> None:
            """Cancel queued tasks so the pool empties fast; cancelled ones are held back and started again."""
            for future, _ in inflight.values():
                future.cancel()

        for row in rows.values():
            emitter.emit(
                "fn.queued",
                function=row.function,
                bytes=row.bytes,
                versions=list(row.versions),
                carryover=row.carryover,
                best_percent=row.best_percent,
            )
            start(row)
        try:
            while not steps_error and not stopper.reached(rows):
                try:
                    kind, payload = inbox.get(timeout=stopper.timeout(rows))
                except queue.Empty:
                    continue
                if kind in ("draft", "compare", "search"):
                    function, done = payload
                    entry = inflight.get(function)
                    if entry is None or entry[0] is not done:
                        continue  # superseded by a newer task for the row
                    del inflight[function]
                    if done.cancelled():
                        deferred.setdefault(function, entry[1])
                    elif kind == "draft":
                        _drafted(rows[function], _result(done), project, emitter, next_words, start, stopper)
                    elif kind == "search":
                        if function not in deferred:  # a newer edit waits: this search read an older text
                            searched(rows[function], _result(done))
                    else:
                        outcome = _compared(rows[function], _result(done), emitter, stopper)
                        if outcome == "exact":
                            rows[function].stage = "landing"
                            exact.append(function)
                            drain()
                        elif outcome == "short" and function not in deferred:
                            climb(rows[function], rows[function].best_percent or 0.0)
                    if function in deferred and not exact:
                        submit(function, deferred[function])
                elif kind == "edit":
                    path = Path(payload)
                    edited = rows.get(path.stem)
                    if edited is None or edited.stage in ("landed", "landing") or edited.held or not path.is_file():
                        continue
                    row = edited
                    sha = _sha(path)
                    if sha == row.sha256:
                        continue
                    row.file, row.sha256 = str(path), sha
                    row.ladder = ladder.Ladder()
                    stopper.note_activity()
                    emitter.emit("fn.edit", function=row.function, file=row.file, sha256=sha)
                    old = inflight.get(row.function)
                    if old is not None:
                        old[0].cancel()  # a running compare finishes first; its result no longer matches the file
                    row.stage = "comparing"
                    emitter.emit("fn.compare.start", function=row.function, sha256=sha)
                    submit(row.function, Task("compare", row.file))
                elif kind == "key":
                    _key(payload, rows, start, emitter, next_words)
                elif kind == "quit":
                    break
                if exact and not inflight:
                    land_drained(resume=True)
        except KeyboardInterrupt:
            exit_code = 130
        finally:
            watcher_stop.set()
            if exit_code != 130:
                # Running tasks finish before the pool closes; an exact row still waiting lands now.
                drain()
                wait([future for future, _ in inflight.values()])
                inflight.clear()
                if exact:
                    land_drained(resume=False)
            if board is not None:
                board.close()
    from unbake import config

    recorded = land.record(config.load(project.root), host)
    if recorded is not None:
        commit, functions = recorded
        emitter.emit("cycle.committed", commit=commit, message="Record attempts", functions=list(functions))
    if exit_code != 130:
        # Commit what the steps wrote so the tree is left clean.
        settled = land.commit_generated(project, host, before, "Refresh generated files")
        if settled is not None:
            emitter.emit("cycle.committed", commit=settled, message="Refresh generated files", functions=[])
    held = [name for name, row in rows.items() if row.stage in ("held", "failed")]
    carry = [name for name, row in rows.items() if row.stage not in ("landed", "held", "failed")]
    if exit_code == 0 and (held or carry or steps_error or regressed):
        exit_code = 1
    for name, row in rows.items():
        if row.stage != "landed":
            tui.line(f"{name}: {row.stage}: {row.diagnostic or 'no diagnostic'}")
    following = next_words("cycle", "--pick", str(max(1, len(rows))), "--stop", stopper.condition)
    emitter.emit(
        "cycle.end",
        landed=landed,
        landed_bytes=sum(rows[name].bytes for name in landed),
        held=held,
        carryovers=carry,
        exit=exit_code,
        next=following,
    )
    atomic_files.text(
        state_path(project),
        json.dumps({"running": False, "rows": [asdict(r) for r in rows.values()]}, sort_keys=True, default=list) + "\n",
    )
    data = {
        "landed": landed,
        "held": held,
        "carryovers": carry,
        "regressed": regressed,
        "exit": exit_code,
    }
    lines = [f"landed {len(landed)}; held {len(held)}; carried over {len(carry)}"]
    if steps_error:
        lines.append(f"steps: {steps_error}")
    lines.extend(
        f"regressed: {row['function']} no longer matches after its land's steps: {row['diagnostic']}"
        for row in regressed
    )
    if exit_code == 130:
        return Result.held("cycle", Held("cycle", "interrupted: stopped by the user"), following, data)
    if steps_error:
        return Result("cycle", "held", "cycle.steps", data, following, tuple(lines))
    if regressed:
        return Result("cycle", "held", "cycle.regressed", data, following, tuple(lines))
    if exit_code:
        return Result("cycle", "held", "cycle.incomplete", data, following, tuple(lines))
    return Result.ok("cycle", data, lines, following)


def _drafted(
    row: Row,
    result: dict[str, Any],
    project: Project,
    emitter: Emitter,
    next_words: Callable[..., str],
    start: Callable[..., None],
    stopper: Stop,
) -> None:
    function = row.function
    if not result["ok"]:
        written = project.work / function / f"{function}.c"
        emitter.emit(
            "fn.draft.done",
            function=function,
            ok=False,
            file=str(written) if written.is_file() else "",
            seconds=round(result["seconds"], 3),
            diagnostic=result["diagnostic"],
            **({"fault": result["fault"]} if "fault" in result else {}),
        )
        if written.is_file():
            # An unproven draft was written: compare it, then wait for edits like any other.
            row.drafted_sha = _sha(written)
            stopper.note_activity()
            start(row)
            return
        row.stage, row.diagnostic = "held", result["diagnostic"]
        emitter.emit(
            "fn.held",
            function=function,
            key=result["key"],
            reason=result["diagnostic"],
            **({"fault": result["fault"]} if "fault" in result else {}),
            next=next_words("draft", function),
        )
        return
    emitter.emit("fn.draft.done", function=function, ok=True, file=result["file"], seconds=round(result["seconds"], 3))
    stopper.note_activity()
    row.drafted_sha = _sha(Path(result["file"]))
    start(row)


def _compared(row: Row, result: dict[str, Any], emitter: Emitter, stopper: Stop) -> str:
    """Record a compare: "exact" when the row should land, "short" when the text compiles and links but differs
    (the ladder's turn), else "" (refused, or the file changed meanwhile)."""
    row.tries += 1
    if not result["ok"]:
        row.stage, row.diagnostic = "waiting for edit", result["diagnostic"]
        row.best_percent = None
        emitter.emit(
            "fn.compare.done",
            function=row.function,
            sha256=row.sha256,
            per_version=result.get("per_version", {}),
            best_percent=None,
            tries=row.tries,
            seconds=round(result["seconds"], 3),
            diagnostic=result["diagnostic"],
            **({"fault": result["fault"]} if "fault" in result else {}),
        )
        return ""
    if result["sha256"] != row.sha256:
        return ""  # the file changed while it was compared; the watcher queued a new compare
    row.best_percent = result["best_percent"]
    row.diagnostic = result["diagnostic"]
    emitter.emit(
        "fn.compare.done",
        function=row.function,
        sha256=row.sha256,
        per_version=result["per_version"],
        best_percent=result["best_percent"],
        tries=row.tries,
        seconds=round(result["seconds"], 3),
        diagnostic=result["diagnostic"],
        **({"fault": result["fault"]} if "fault" in result else {}),
    )
    stopper.note_activity()
    if result["exact"]:
        emitter.emit("fn.exact", function=row.function, bytes=row.bytes, sha256=row.sha256)
        return "exact"
    row.stage = "waiting for edit"
    return "short" if row.best_percent < 100 else ""


def _result(done: Future[dict[str, Any]]) -> dict[str, Any]:
    from unbake.pool import WorkerMemory

    if done.cancelled():
        return {"ok": False, "key": "cycle.cancelled", "diagnostic": "superseded", "seconds": 0.0}
    error = done.exception()
    if error is not None:
        key = (
            error.key
            if isinstance(error, Held)
            else "worker.memory"
            if isinstance(error, MemoryError)
            else "worker.crash"
        )
        return {
            "ok": False,
            "key": key,
            "diagnostic": f"{type(error).__name__}: {error}",
            "seconds": (
                (error.fault or {}).get("wall_seconds")
                if isinstance(error, Held)
                else error.args[0].get("wall_seconds")
                if isinstance(error, WorkerMemory)
                else None
            ),
            "fault": cause_fault(error),
        }
    return done.result()


def _key(
    key: tuple[str, str],
    rows: dict[str, Row],
    start: Callable[..., None],
    emitter: Emitter,
    next_words: Callable[..., str],
) -> None:
    """Board keys that change work: r (compare again), d (redraft), h (hold or release)."""
    action, function = key
    row = rows.get(function)
    if row is None:
        return
    if action == "r" and not row.held:
        row.sha256 = ""
        start(row)
    elif action == "d" and not row.held:
        start(row, redraft=True)
    elif action == "h":
        row.held = not row.held
        if row.held:
            row.stage = "held"
            emitter.emit(
                "fn.held",
                function=function,
                key="cycle.held_by_user",
                reason="held from the board",
                next=next_words("cycle", "--functions", function),
            )
        else:
            row.stage = "queued"
            start(row)
