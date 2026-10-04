"""The cycle coordinator: pick, draft and compare in worker processes, land each exact function at once.

One thread owns all shared state and waits on one queue. Messages come from worker futures (draft and
compare results), the file watcher (saved drafts), the push thread and, on a terminal, the key reader.
Lands run on this thread, one at a time, in arrival order. Nothing polls: the only timed wait is the
stop condition's own deadline.
"""

from __future__ import annotations

import hashlib
import json
import queue
import sys
import threading
import time
from collections.abc import Callable
from concurrent.futures import Future
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, TextIO

from unbake import atomic as atomic_files
from unbake.cli.output import Result
from unbake.config import Held, Host, Project
from unbake.cycle import rank
from unbake.cycle.events import Emitter

STAGES = ("queued", "drafting", "comparing", "waiting for edit", "landing", "landed", "held", "failed")


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


# ---- worker tasks (module level so the fork server can import them) ----


def _draft_task(spec: tuple[Path, Host, str, bool]) -> dict[str, Any]:
    from unbake import config
    from unbake.work import draft

    root, host, function, replace = spec
    started = time.monotonic()
    try:
        made = draft.draft(config.load(root), host, function, replace=replace)
    except Held as error:
        return {"ok": False, "key": error.key, "diagnostic": error.reason, "seconds": time.monotonic() - started}
    return {"ok": True, "file": str(made.file), "seconds": time.monotonic() - started}


def _compare_task(spec: tuple[Path, Host, str]) -> dict[str, Any]:
    from unbake import config
    from unbake.work import compare

    root, host, file = spec
    started = time.monotonic()
    try:
        measured = compare.compare(config.load(root), host, Path(file))
    except Held as error:
        return {"ok": False, "key": error.key, "diagnostic": error.reason, "seconds": time.monotonic() - started}
    return {
        "ok": True,
        "sha256": measured.source_sha256,
        "per_version": {
            v: {"percent": round(c.match_percent, 6), "exact": c.exact} for v, c in measured.compares.items()
        },
        "best_percent": measured.best_percent,
        "exact": measured.exact,
        "diagnostic": next((line for c in measured.compares.values() for line in c.lines[2:3]), ""),
        "seconds": time.monotonic() - started,
    }


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
    return sys.stdin.isatty() and sys.stderr.isatty()


# ---- the run ----


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class Stop:
    def __init__(self, condition: str | None) -> None:
        self.condition = condition or "all-landed"
        self.started = time.monotonic()
        self.activity = self.started

    def note_activity(self) -> None:
        self.activity = time.monotonic()

    def timeout(self) -> float | None:
        kind, _, value = self.condition.partition(":")
        if kind == "idle":
            return max(0.0, self.activity + int(value) - time.monotonic())
        if kind == "after":
            return max(0.0, self.started + int(value) * 60 - time.monotonic())
        return None

    def reached(self, rows: dict[str, Row]) -> bool:
        if self.condition == "all-landed":
            return all(row.stage in ("landed", "held", "failed") for row in rows.values())
        timeout = self.timeout()
        return timeout is not None and timeout <= 0


def choose(project: Project, host: Host, pick: int | None, functions: tuple[str, ...]) -> list[rank.Candidate]:
    order = ranked(project, host)
    if functions:
        by_name = {row.function: row for row in order}
        missing = [name for name in functions if name not in by_name]
        if missing:
            raise Held(
                "cycle",
                f"cycle.functions: {', '.join(missing)}: "
                "not candidates (published, unknown or outside the size window)",
            )
        return [by_name[name] for name in functions]
    if pick is not None:
        return order[:pick]
    from unbake.cycle import ui

    return ui.pick(order)


def run(
    project: Project,
    host: Host,
    *,
    pick: int | None,
    functions: tuple[str, ...],
    stop: str | None,
    push: bool,
    events: TextIO,
    next_words: Callable[..., str],
) -> Result:
    from unbake import land, pool, steps

    picked = choose(project, host, pick, functions)
    if not picked:
        raise Held("cycle", "cycle.pick: nothing to work on (no candidates in the size window)")
    if not interactive():
        print(
            "→ "
            + next_words(
                "cycle", "--functions", ",".join(row.function for row in picked), "--stop", stop or "all-landed"
            ),
            file=sys.stderr,
        )
    emitter = Emitter(events)
    rows = {c.function: Row(c.function, c.bytes, c.versions, c.carryover, best_percent=c.best_percent) for c in picked}
    inbox: queue.Queue[tuple[str, Any]] = queue.Queue()
    stopper = Stop(stop)
    board = None
    if interactive():
        from unbake.cycle import ui

        board = ui.Board(project, rows, inbox)
        emitter.listeners.append(board.on_event)

    def save_state(_: dict[str, Any] | None = None) -> None:
        state = {"running": True, "rows": [asdict(row) for row in rows.values()]}
        atomic_files.text(state_path(project), json.dumps(state, sort_keys=True, default=list) + "\n")

    emitter.listeners.append(save_state)
    for done_step in steps.ensure(project, host, ["extract", "types", "headers", "buildfiles"]):
        if done_step.ran:
            emitter.emit(
                "step.run", step=done_step.step, trigger=done_step.trigger, seconds=round(done_step.seconds, 3)
            )
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
        cache_root=str(host.cache_root),
        stop=stopper.condition,
        remote=host.publish_remote if push else "",
        branch=host.publish_branch if push else "",
    )
    unpushed: list[str] = []
    landed: list[str] = []
    pusher = land.Pusher(project, host, lambda ok: inbox.put(("pushed", ok))) if push else None
    inflight: dict[str, Future[dict[str, Any]]] = {}
    retried: set[tuple[str, str]] = set()
    watcher_stop = threading.Event()
    from unbake.cycle import watcher

    watching = threading.Thread(
        target=watcher.watch, args=(project.work, host.cycle_debounce_ms, watcher_stop, inbox), daemon=True
    )
    watching.start()
    exit_code = 0

    with pool.Pool.from_host(host) as workers:

        def submit(kind: str, function: str, argument: str, *, replace: bool = False) -> None:
            if kind == "draft":
                future = workers.submit(_draft_task, (project.root, host, argument, replace))
            else:
                future = workers.submit(_compare_task, (project.root, host, argument))
            inflight[function] = future

            def finished(done: Future[Any]) -> None:
                inbox.put((kind, (function, argument, done)))

            future.add_done_callback(finished)

        def start(row: Row, *, redraft: bool = False) -> None:
            if row.held:
                return
            file = project.work / row.function / f"{row.function}.c"
            if redraft:
                row.stage, row.sha256 = "drafting", ""
                emitter.emit("fn.draft.start", function=row.function)
                submit("draft", row.function, row.function, replace=True)
            elif file.is_file():
                row.file = str(file)
                row.sha256 = _sha(file)
                row.stage = "comparing"
                emitter.emit("fn.compare.start", function=row.function, sha256=row.sha256)
                submit("compare", row.function, str(file))
            else:
                row.stage = "drafting"
                emitter.emit("fn.draft.start", function=row.function)
                submit("draft", row.function, row.function)

        def finish_land(row: Row) -> None:
            row.stage = "landing"
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
            emitter.emit(
                "fn.landed",
                function=row.function,
                bytes=row.bytes,
                versions=list(row.versions),
                seconds=round(time.monotonic() - started, 3),
                retried=(row.function, row.sha256) in retried,
            )
            emitter.emit("fn.committed", function=row.function, commit=commit, message=f"Match {row.function}")
            unpushed.append(commit)
            for merged in steps.ensure(project, host, ["merge-units"]):
                if merged.ran:
                    emitter.emit("step.run", step=merged.step, trigger=merged.trigger, seconds=round(merged.seconds, 3))
            if pusher is not None:
                pusher.request()

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
            while not stopper.reached(rows):
                try:
                    kind, payload = inbox.get(timeout=stopper.timeout())
                except queue.Empty:
                    continue
                if kind == "draft":
                    function, _, done = payload
                    row = rows[function]
                    result = _result(done)
                    if not result["ok"]:
                        row.stage, row.diagnostic = "held", result["diagnostic"]
                        emitter.emit(
                            "fn.draft.done",
                            function=function,
                            ok=False,
                            file="",
                            seconds=round(result["seconds"], 3),
                            diagnostic=result["diagnostic"],
                        )
                        emitter.emit(
                            "fn.held",
                            function=function,
                            key=result["key"],
                            reason=result["diagnostic"],
                            next=next_words("draft", function),
                        )
                        continue
                    emitter.emit(
                        "fn.draft.done",
                        function=function,
                        ok=True,
                        file=result["file"],
                        seconds=round(result["seconds"], 3),
                    )
                    stopper.note_activity()
                    start(row)
                elif kind == "compare":
                    function, _file, done = payload
                    row = rows[function]
                    result = _result(done)
                    if inflight.get(function) is not done:
                        continue  # superseded by a newer save
                    row.tries += 1
                    if not result["ok"]:
                        row.stage, row.diagnostic = "waiting for edit", result["diagnostic"]
                        emitter.emit(
                            "fn.compare.done",
                            function=function,
                            sha256=row.sha256,
                            per_version={},
                            best_percent=0.0,
                            tries=row.tries,
                            seconds=round(result["seconds"], 3),
                            diagnostic=result["diagnostic"],
                        )
                        continue
                    if result["sha256"] != row.sha256:
                        continue  # the file changed while it was compared; the watcher queued a new compare
                    row.best_percent = result["best_percent"]
                    row.diagnostic = result["diagnostic"]
                    emitter.emit(
                        "fn.compare.done",
                        function=function,
                        sha256=row.sha256,
                        per_version=result["per_version"],
                        best_percent=result["best_percent"],
                        tries=row.tries,
                        seconds=round(result["seconds"], 3),
                    )
                    stopper.note_activity()
                    if result["exact"]:
                        emitter.emit("fn.exact", function=function, bytes=row.bytes, sha256=row.sha256)
                        finish_land(row)
                    else:
                        row.stage = "waiting for edit"
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
                    stopper.note_activity()
                    emitter.emit("fn.edit", function=row.function, file=row.file, sha256=sha)
                    old = inflight.get(row.function)
                    if old is not None:
                        old.cancel()
                    row.stage = "comparing"
                    emitter.emit("fn.compare.start", function=row.function, sha256=sha)
                    submit("compare", row.function, row.file)
                elif kind == "pushed":
                    emitter.emit(
                        "fn.pushed",
                        commits=list(unpushed),
                        remote=host.publish_remote,
                        branch=host.publish_branch,
                        ok=bool(payload),
                        **({} if payload else {"error": "git push failed"}),
                    )
                    if payload:
                        unpushed.clear()
                elif kind == "key":
                    _key(payload, rows, start, emitter, next_words)
                elif kind == "quit":
                    break
        except KeyboardInterrupt:
            exit_code = 130
        finally:
            watcher_stop.set()
            if board is not None:
                board.close()
    if pusher is not None:
        pusher.wait()
        if unpushed:
            pusher.request()
            pusher.wait()
        while True:
            try:
                kind, payload = inbox.get_nowait()
            except queue.Empty:
                break
            if kind == "pushed":
                emitter.emit(
                    "fn.pushed",
                    commits=list(unpushed),
                    remote=host.publish_remote,
                    branch=host.publish_branch,
                    ok=bool(payload),
                    **({} if payload else {"error": "git push failed"}),
                )
                if payload:
                    unpushed.clear()
    held = [name for name, row in rows.items() if row.stage in ("held", "failed")]
    carry = [name for name, row in rows.items() if row.stage not in ("landed", "held", "failed")]
    if exit_code == 0 and (held or unpushed or carry):
        exit_code = 1
    following = next_words("cycle", "--pick", str(max(1, len(rows))), "--stop", stopper.condition)
    emitter.emit(
        "cycle.end",
        landed=landed,
        landed_bytes=sum(rows[name].bytes for name in landed),
        unpushed=list(unpushed),
        held=held,
        carryovers=carry,
        exit=exit_code,
        next=following,
    )
    atomic_files.text(
        state_path(project),
        json.dumps({"running": False, "rows": [asdict(r) for r in rows.values()]}, sort_keys=True, default=list) + "\n",
    )
    data = {"landed": landed, "unpushed": unpushed, "held": held, "carryovers": carry, "exit": exit_code}
    lines = [f"landed {len(landed)}; held {len(held)}; carried over {len(carry)}; unpushed {len(unpushed)}"]
    if exit_code == 130:
        return Result.held("cycle", Held("cycle", "interrupted: stopped by the user"), following, data)
    if exit_code:
        return Result("cycle", "held", "cycle.incomplete", data, following, tuple(lines))
    return Result.ok("cycle", data, lines, following)


def _result(done: Future[dict[str, Any]]) -> dict[str, Any]:
    if done.cancelled():
        return {"ok": False, "key": "cycle.cancelled", "diagnostic": "superseded", "seconds": 0.0}
    error = done.exception()
    if error is not None:
        key = "worker.memory" if isinstance(error, MemoryError) else "worker.crash"
        return {"ok": False, "key": key, "diagnostic": f"{type(error).__name__}: {error}", "seconds": 0.0}
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
