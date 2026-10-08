"""The worker pool for CPU work: forkserver processes, memory admission, recycling, one retry.

Workers start from a fork server created before any project data is loaded, so they start small.
Each worker caps its own data segment at memory_worker_bytes (RLIMIT_DATA, inherited by the tools it
runs). A task whose worker dies or runs out of memory is retried once in a fresh worker; a second
failure raises TaskFailed naming worker.crash or worker.memory.

Each worker leads its own process group, so the compilers and tools it starts share it. A worker thread
waits on a pidfd of the process that owns the pool; when that process dies, even by SIGKILL, the worker kills
the fork server and its own group. While a pool is open, SIGINT, SIGTERM and SIGHUP kill every worker group at
once and then raise in the main thread (KeyboardInterrupt, or SystemExit 128+N); leaving the pool on an
exception kills the groups instead of waiting for running tasks.
"""

from __future__ import annotations

import atexit
import contextlib
import json
import multiprocessing
import multiprocessing.connection
import os
import pickle
import queue
import resource
import shutil
import signal
import statistics
import sys
import tempfile
import threading
import time
import traceback
import uuid
from collections import deque
from collections.abc import Callable, Iterator, Sequence
from concurrent.futures import Future, ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from dataclasses import asdict, dataclass
from pathlib import Path
from types import FrameType
from typing import Any, TypeVar, cast

from unbake import atomic as atomic_files
from unbake import process, tui
from unbake.config import Held, Host
from unbake.process import Fault, Frame, RetryRule, temporary_environment
from unbake.process import named as cause_named

T = TypeVar("T")
R = TypeVar("R")

RECYCLE_AFTER = 64
# run() batches items into jobs: at most this many items per job (neighbouring items share a worker's memos),
# and at least this many jobs per worker so a small fill still spreads over every worker.
ITEMS_PER_JOB = 8
JOBS_PER_WORKER = 4
SIGNALS = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)


@dataclass(frozen=True)
class TaskIdentity:
    action: str
    source: str | None
    functions: tuple[str, ...]
    versions: tuple[str, ...]
    source_bytes: int | None
    source_sha256: str | None
    input_keys: tuple[str, ...]


class TaskFailed(Held):
    def __init__(self, key: str, fault: dict[str, Any]) -> None:
        identity = fault.get("identity", {})
        source = identity.get("source") or fault["action"]
        functions = ", ".join(identity.get("functions", ()))
        versions = ", ".join(identity.get("versions", ()))
        where = fault.get("allocation", fault.get("cause", "worker exited"))
        suffix = "; retry failed" if fault.get("retry_exhausted", True) else ""
        from unbake.inputs import DependencySet

        dependencies = DependencySet(
            (),
            {
                "input_keys": identity.get("input_keys", []),
                "source_sha256": identity.get("source_sha256"),
                "memory_worker_bytes": fault.get("configured_cap_bytes", fault.get("cap_bytes")),
                "dependencies_unknown": not bool(identity.get("input_keys")),
            },
            {},
        )
        rule = RetryRule(
            "dependencies" if key == "worker.memory" else "worker-generation",
            ("value:input_keys", "value:source_sha256", "value:memory_worker_bytes")
            if key == "worker.memory"
            else ("value:worker_generation",),
        )
        super().__init__(
            Fault(
                cause_named(
                    key,
                    f"{source} {functions} ({versions}): {where}{suffix}",
                    owner="pool",
                    stage=str(where),
                    subject=str(source),
                    dependencies=dependencies,
                    retry=rule,
                    evidence=fault,
                ),
                (Frame("context", "pool", "worker", "worker transport", fault),),
            )
        )
        self.failure = key
        self.completed: list[tuple[TaskIdentity, Any]] = []


@dataclass
class _Current:
    identity: TaskIdentity
    phase: str
    pid: int | None = None
    started: float = 0.0
    last: float = 0.0
    stage: str = "queued"


class Watchdog:
    """Only worker start/progress events advance a running task; queued work has no deadline.

    Use eight times the phase's recent completed-unit median, with a five-second
    stabilization floor. Before the first completion allow sixty seconds. The
    clock is supplied explicitly so tests can advance work without waiting.
    """

    def __init__(
        self,
        *,
        multiplier: float = 8,
        minimum: float = 5,
        bootstrap: float = 60,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if multiplier <= 0 or minimum < 0 or bootstrap <= 0:
            raise ValueError("pool.watchdog: positive multiplier/bootstrap and nonnegative minimum required")
        self.multiplier, self.minimum, self.bootstrap, self.clock = multiplier, minimum, bootstrap, clock
        self.current: dict[str, _Current] = {}
        self.samples: dict[str, deque[float]] = {}

    def queued(self, token: str, identity: TaskIdentity, phase: str) -> None:
        self.current[token] = _Current(identity, phase)

    def update(self, token: str, state: str, pid: int, identity: TaskIdentity | None, stage: str) -> None:
        current = self.current.get(token)
        if current is None:
            return
        now = self.clock()
        if identity is not None:
            current.identity = identity
        if state == "start":
            current.started = now
        elif state == "item-done":
            self.samples.setdefault(current.phase, deque(maxlen=64)).append(max(0, now - current.started))
        current.pid, current.last, current.stage = pid, now, stage

    def finished(self, token: str) -> _Current | None:
        return self.current.pop(token, None)

    def expired(self) -> list[tuple[str, TaskFailed]]:
        found = []
        now = self.clock()
        for token, current in self.current.items():
            if current.pid is None:
                continue
            samples = self.samples.get(current.phase)
            median = statistics.median(samples) if samples else None
            limit = self.bootstrap if median is None else self.multiplier * max(self.minimum, median)
            idle = now - current.last
            if idle < limit:
                continue
            found.append(
                (
                    token,
                    TaskFailed(
                        "worker.stuck",
                        {
                            "action": current.identity.action,
                            "identity": asdict(current.identity),
                            "category": "worker-stuck",
                            "cause": "no task progress",
                            "retry_exhausted": False,
                            "worker_pid": current.pid,
                            "stage": current.stage,
                            "phase": current.phase,
                            "no_progress_seconds": idle,
                            "threshold_seconds": limit,
                            "phase_median_seconds": median,
                            "multiplier": self.multiplier,
                            "minimum_seconds": self.minimum,
                            "cpu_seconds": None,
                            "peak_rss_bytes": None,
                            "counts": None,
                        },
                    ),
                )
            )
        return found


@dataclass(frozen=True)
class _Submission:
    # This token routes events to the existing future; TaskIdentity is the input identity.
    fn: Any
    token: str
    identity: TaskIdentity


_events: Any = None
_progress_root: str | None = None
_token: str | None = None
_identity: TaskIdentity | None = None


def current_identity() -> TaskIdentity | None:
    return _identity if _token is not None else None


def _notify(state: str, stage: str, identity: TaskIdentity | None = None) -> None:
    global _identity
    if _token is None or _events is None:
        return
    if identity is not None:
        _identity = identity
    payload = pickle.dumps((_token, state, os.getpid(), identity, stage), protocol=pickle.HIGHEST_PROTOCOL)
    # A killed writer must not leave a partial shared-pipe message that blocks
    # the watchdog itself. Large identities use the same explicit scratch transport.
    if len(payload) > 3000:
        if _progress_root is None:
            raise Held(
                cause_named(
                    "pool.progress",
                    "pool.progress: explicit scratch required for a large TaskIdentity",
                    owner="pool",
                    stage="pool",
                )
            )
        path = Path(_progress_root) / (uuid.uuid4().hex + ".pickle")
        atomic_files.fresh(path, payload)
        payload = pickle.dumps(("file", str(path)))
    _events.put(payload)


def progress(identity: TaskIdentity | None = None, *, step: str = "work") -> None:
    """Report actual work boundaries, never an automatic heartbeat for a stuck computation."""
    _notify("progress", step, identity)


def _identify(fn: Any, shared: Any, item: Any) -> TaskIdentity:
    from unbake import effort

    identify = getattr(fn, "_pool_identity", None)
    return (
        identify(shared, item)
        if identify is not None
        else TaskIdentity(effort.name_of(fn), None, (), (), None, None, ())
    )


def admitted(workers: int, memory_total_bytes: int, memory_parent_bytes: int, memory_worker_bytes: int) -> int:
    """How many workers fit: at most `workers`, and their caps fit within total - parent."""
    if memory_worker_bytes <= 0 or memory_total_bytes <= memory_parent_bytes:
        raise Held(
            cause_named(
                "pool.memory",
                "pool.memory: worker cap and total-parent budget must be positive",
                owner="pool",
                stage="pool",
            )
        )
    return max(1, min(workers, (memory_total_bytes - memory_parent_bytes) // memory_worker_bytes))


def _die_with_owner() -> None:
    """Wait on the owner's pidfd in a thread; when it exits, kill the fork server and this group."""
    server = os.getppid()
    try:
        descriptor: int | None = os.pidfd_open(process.owner(server))
    except (ProcessLookupError, FileNotFoundError):
        descriptor = None
    threading.Thread(target=process.orphaned, args=(descriptor, server), name="owner", daemon=True).start()


def _cap(
    memory_worker_bytes: int,
    directory: str,
    cache_memory_bytes: int | None,
    events: Any = None,
    progress_root: str | None = None,
) -> None:
    """Worker start: lead a new process group, die with the pool's owner, cap the data segment."""
    from unbake import cache

    global _events, _progress_root
    _events, _progress_root = events, progress_root
    if cache_memory_bytes is not None:
        cache.configure(memory_bytes=cache_memory_bytes)
    os.setpgid(0, 0)
    if sys.platform == "linux":
        _die_with_owner()
    signal.signal(signal.SIGUSR1, _release_finished_payload)
    resource.setrlimit(resource.RLIMIT_DATA, (memory_worker_bytes, memory_worker_bytes))
    # Python/native dependencies in a worker must also avoid inherited system temp.
    os.environ.update(temporary_environment(Path(directory)))
    tempfile.tempdir = directory
    if sys.platform == "linux":
        multiprocessing.connection.arbitrary_address = _socket_address  # type: ignore[attr-defined]


# The fork server listens on a socket in multiprocessing's private temp dir; sun_path holds 107 bytes.
_SOCKET_ROOM = 107 - len("/pymp-xxxxxxxx/listener-xxxxxxxx")


_arbitrary_address = multiprocessing.connection.arbitrary_address  # type: ignore[attr-defined]


def _socket_address(family: str) -> Any:
    """Linux abstract sockets carry no files and do not constrain configured cache paths."""
    if family == "AF_UNIX":
        return b"\0unbake-" + uuid.uuid4().hex.encode()
    return _arbitrary_address(family)


def _socket_directory(root: Path) -> str:
    """Keep multiprocessing transport under explicit storage, even with an inherited tempdir."""
    root = root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    config = multiprocessing.process.current_process()._config  # type: ignore[attr-defined]
    if sys.platform == "linux":
        multiprocessing.connection.arbitrary_address = _socket_address  # type: ignore[attr-defined]
    elif len(os.fsencode(root)) > _SOCKET_ROOM:
        raise Held(
            cause_named(
                "pool.socket",
                f"pool.socket: cache.machine_root {root} is too long for the worker socket; configure a shorter path",
                owner="pool",
                stage="pool",
            )
        )
    previous = config.get("tempdir")
    if previous and Path(previous).parent == root and Path(previous).is_dir():
        return str(previous)
    directory = tempfile.mkdtemp(prefix="pymp-", dir=root)
    atexit.register(shutil.rmtree, directory, ignore_errors=True)
    config["tempdir"] = directory
    return directory


def _executor(
    size: int,
    memory_worker_bytes: int,
    work_per_job: int,
    scratch: Path | None = None,
    cache_memory_bytes: int | None = None,
    events: Any = None,
    progress_root: str | None = None,
) -> ProcessPoolExecutor:
    if scratch is None:
        raise Held(
            cause_named(
                "pool.scratch",
                "pool.scratch: cache.machine_root is required for worker transport",
                owner="pool",
                stage="pool",
            )
        )
    directory = _socket_directory(scratch)
    return ProcessPoolExecutor(
        max_workers=size,
        mp_context=multiprocessing.get_context("forkserver"),
        initializer=_cap,
        initargs=(memory_worker_bytes, directory, cache_memory_bytes)
        + (() if events is None else (events, progress_root)),
        max_tasks_per_child=max(1, RECYCLE_AFTER // work_per_job),
    )


def _cpu() -> float:
    own, children = resource.getrusage(resource.RUSAGE_SELF), resource.getrusage(resource.RUSAGE_CHILDREN)
    return own.ru_utime + own.ru_stime + children.ru_utime + children.ru_stime


class WorkerMemory(MemoryError):
    """The measured allocating action, including any known physical input identity."""


def memory_fault(
    error: BaseException, identity: TaskIdentity | None = None, *, expanded_bytes: int | None = None
) -> dict[str, Any]:
    from dataclasses import asdict

    from unbake import effort

    observed: dict[str, int | None] = {"vmdata_bytes": None, "rss_bytes": None}
    try:
        for line in Path("/proc/self/status").read_text().splitlines():
            for native, field in (("VmData:", "vmdata_bytes"), ("VmRSS:", "rss_bytes")):
                if line.startswith(native):
                    observed[field] = int(line.split()[1]) * 1024
    except OSError:
        pass
    return {
        "category": "allocation",
        "action": "types" if identity is not None else "pool",
        "identity": asdict(identity) if identity is not None else {},
        "allocation": _where(error),
        "cause": type(error).__name__,
        "cap_bytes": None
        if resource.getrlimit(resource.RLIMIT_DATA)[0] == resource.RLIM_INFINITY
        else resource.getrlimit(resource.RLIMIT_DATA)[0],
        "peak_rss_bytes": effort.resident_peak(),
        "expanded_bytes": expanded_bytes,
        **observed,
    }


def _where(error: BaseException) -> str:
    """The innermost frame the tool owns (else the innermost frame) that raised ERROR, as file:line."""
    frames = traceback.extract_tb(error.__traceback__)
    owned = [frame for frame in frames if "/unbake/" in frame.filename and not frame.filename.endswith("/pool.py")]
    chosen = owned or list(frames)
    return f"{Path(chosen[-1].filename).name}:{chosen[-1].lineno}" if chosen else "an unknown place"


def _named(fn: Callable[..., R], *arguments: Any) -> R:
    """fn(*arguments) in a worker: a refusal passes as itself, a crash becomes a refusal naming the function, the
    exception and where it was raised (the worker's traceback never reaches the user)."""
    from unbake import effort

    try:
        return fn(*arguments)
    except WorkerMemory:
        raise
    except MemoryError as error:
        raise WorkerMemory(memory_fault(error)) from error
    except Held:
        raise
    except Exception as error:
        raise Held(
            Fault(
                cause_named(
                    f"{effort.name_of(fn)}",
                    f"{effort.name_of(fn)}: {type(error).__name__} at {_where(error)}: {error}",
                    owner="pool",
                    stage="pool",
                ),
                (
                    Frame(
                        "context",
                        "pool",
                        "pool",
                        f"{effort.name_of(fn)}: {type(error).__name__} at {_where(error)}: {error}",
                        {
                            "action": effort.name_of(fn),
                            "category": "python",
                            "cause": type(error).__name__,
                            "where": _where(error),
                        },
                    ),
                ),
            )
        ) from error


def _measured(
    task: tuple[Callable[[T], R], T],
) -> tuple[R | None, float, int, dict[str, tuple[int, int]], BaseException | None]:
    """Return effort at the action boundary, even when its result is a fault."""
    from unbake import effort

    global _token, _identity
    fn, item = task
    if isinstance(fn, _Submission):
        _token, _identity, fn = fn.token, fn.identity, fn.fn
        _notify("start", "load-shared" if fn is _batch else "item", _identity)
    start, before, wall = _cpu(), effort.counted(), time.monotonic()
    result, error = None, None
    try:
        result = _named(fn, item)
    except Exception as caught:
        error = caught
    added = {}
    for name, (done, total) in effort.counted().items():
        old_done, old_total = before.get(name, (0, 0))
        if total != old_total or done != old_done:
            added[name] = (done - old_done, total - old_total)
    seconds, rss = _cpu() - start, effort.resident_peak()
    measured = {
        "cpu_seconds": seconds,
        "peak_rss_bytes": rss,
        "counts": added,
        "wall_seconds": time.monotonic() - wall,
        "wall_scope": "worker-action",
    }
    if isinstance(error, WorkerMemory):
        error.args[0].update(measured)
    elif isinstance(error, Held):
        error.fault = error.fault.framed("pool", "worker", error.reason, measured)
    if fn is not _batch and error is None:
        _notify("item-done", "sendback")
    else:
        progress(step="sendback")
    _token, _identity = None, None
    return result, seconds, rss, added, error


# The shared value of the last job a worker ran: (path, value). Jobs of one run carry the same path.
_loaded: tuple[str, Any] | None = None


def _release_payload(value: Any) -> None:
    module = sys.modules.get("unbake.typemap.facts")
    if module is not None:
        module.release_payload(value)


def _release_finished_payload(_number: int, _frame: FrameType | None) -> None:
    """An owner completion signal releases only a payload whose private file has ended."""
    global _loaded
    if _loaded is not None and not Path(_loaded[0]).is_file():
        _release_payload(_loaded[1])
        _loaded = None


def _shared_value(path: str) -> Any:
    global _loaded
    if _loaded is None or _loaded[0] != path:
        if _loaded is not None:
            _release_payload(_loaded[1])
        _loaded = (path, pickle.loads(Path(path).read_bytes()))
    return _loaded[1]


def _batch(job: Any) -> list[Any]:
    fn, chunk, path = job[:3]
    identities = job[3] if len(job) == 4 else [None] * len(chunk)
    shared = None if path is None else _shared_value(path)
    result = []
    for item, identity in zip(chunk, identities, strict=True):
        _notify("start", "item", identity)
        result.append(_named(fn, item) if path is None else _named(fn, shared, item))
        _notify("item-done", "sendback")
    return result


def width(count: int, workers: int, per_worker: int = JOBS_PER_WORKER) -> int:
    """Items per job so COUNT items make about WORKERS * PER_WORKER jobs, never an empty one."""
    return max(1, -(-count // (workers * per_worker)))


class _Outer(Future[R]):
    """A task's future: cancelling it cancels the task only while it is still queued."""

    def __init__(self, inner: Future[Any]) -> None:
        super().__init__()
        self._inner = inner

    def cancel(self) -> bool:
        return self._inner.cancel() and super().cancel()


class Pool:
    def __init__(
        self,
        workers: int,
        memory_total_bytes: int,
        memory_parent_bytes: int,
        memory_worker_bytes: int,
        scratch: Path | None = None,
        *,
        cache_memory_bytes: int | None = None,
        watchdog: Watchdog | None = None,
    ):
        self.size = admitted(workers, memory_total_bytes, memory_parent_bytes, memory_worker_bytes)
        self.cache_memory_bytes = cache_memory_bytes
        self.memory_worker_bytes = memory_worker_bytes
        # Where run() leaves a shared value for its workers (the host's machine cache root).
        self.scratch = scratch
        self._executor: ProcessPoolExecutor | None = None
        self._handlers: dict[int, Any] = {}
        self.killed = False
        self._work_per_job = 1
        self.watchdog = watchdog if watchdog is not None else Watchdog()
        self._events: Any = None
        self._progress_directory: Path | None = None
        self._monitor: threading.Thread | None = None
        self._stop = threading.Event()
        self._lock = threading.RLock()
        self._tokens: dict[Future[Any], str] = {}
        self._faults: dict[str, TaskFailed] = {}
        self._identities: dict[str, TaskIdentity] = {}
        self._completed: set[str] = set()
        self._retired: set[str] = set()
        self._phase = "pool"

    @classmethod
    def from_host(cls, host: Host) -> Pool:
        """Use the command's host limits in standalone and broker modes alike."""
        return cls(
            min(host.workers, host.cores),
            host.memory_total_bytes,
            host.memory_parent_bytes,
            host.memory_worker_bytes,
            host.cache_machine_root,
            cache_memory_bytes=host.cache_memory_bytes,
        )

    def __enter__(self) -> Pool:
        if self.scratch is not None:
            self.scratch.mkdir(parents=True, exist_ok=True)
            self._progress_directory = Path(tempfile.mkdtemp(prefix="progress-", dir=self.scratch))
            self._events = multiprocessing.get_context("forkserver").SimpleQueue()
        self._stop.clear()
        self._executor = _executor(
            self.size,
            self.memory_worker_bytes,
            self._work_per_job,
            self.scratch,
            self.cache_memory_bytes,
            self._events,
            None if self._progress_directory is None else str(self._progress_directory),
        )
        if threading.current_thread() is threading.main_thread():
            self._handlers = {number: signal.signal(number, self._signalled) for number in SIGNALS}
        atexit.register(self.kill)
        if self._events is not None:
            self._monitor = threading.Thread(target=self._observe, name="pool-progress", daemon=True)
            self._monitor.start()
        return self

    def __exit__(self, kind: type[BaseException] | None, *exc: object) -> None:
        try:
            if kind is not None or self.killed:
                self.kill()
            elif self._executor is not None:
                self._executor.shutdown(cancel_futures=True)
        finally:
            self._stop.set()
            if self._monitor is not None:
                self._monitor.join()
                self._monitor = None
            if self._events is not None:
                self._events.close()
                self._events = None
            if self._progress_directory is not None:
                shutil.rmtree(self._progress_directory)
                self._progress_directory = None
            with self._lock:
                self.watchdog.current.clear()
                self._tokens.clear()
                self._identities.clear()
                self._faults.clear()
                self._completed.clear()
                self._retired.clear()
            self._executor = None
            atexit.unregister(self.kill)
            for number, previous in self._handlers.items():
                signal.signal(number, previous)
            self._handlers = {}

    def _report(self, token: str, state: str, current: _Current) -> None:
        record = {
            "event": "pool.progress",
            "state": state,
            "pid": current.pid,
            "identity": asdict(current.identity),
            "stage": current.stage,
            "queued": sum(row.pid is None for row in self.watchdog.current.values()),
            "running": sum(row.pid is not None for row in self.watchdog.current.values()),
        }
        with contextlib.suppress(OSError, ValueError):
            tui.write(json.dumps(record) + "\n")
            tui.flush()

    def _observe(self) -> None:
        while not self._stop.is_set():
            if self._events._reader.poll(0.05):
                payload = pickle.loads(self._events.get())
                if payload[0] == "file":
                    path = Path(payload[1])
                    payload = pickle.loads(path.read_bytes())
                    path.unlink()
                token, state, pid, identity, stage = payload
                with self._lock:
                    if state == "finished":
                        current = self.watchdog.finished(token)
                        if current is not None:
                            self._identities[token] = current.identity
                            self._report(token, state, current)
                        if token in self._retired:
                            self._retired.discard(token)
                            self._completed.discard(token)
                            self._identities.pop(token, None)
                            self._faults.pop(token, None)
                    else:
                        self.watchdog.update(token, state, pid, identity, stage)
                        current = self.watchdog.current.get(token)
                        if current is not None:
                            self._identities[token] = current.identity
                            self._report(token, "running" if state == "start" else state, current)
            with self._lock:
                for token, fault in self.watchdog.expired():
                    if token in self._faults or token in self._completed:
                        continue
                    self._faults[token] = fault
                    assert fault.fault is not None
                    fault.fault = fault.fault.framed(
                        "pool", "worker", "configured worker cap", {"configured_cap_bytes": self.memory_worker_bytes}
                    )
                    current = self.watchdog.current[token]
                    self._report(token, "stuck", current)
                    process.kill_groups([cast(int, current.pid)])

    def _failure(self, future: Future[Any], error: BaseException | None = None) -> TaskFailed | None:
        with self._lock:
            token = self._tokens.get(future)
            if token is None:
                return None
            stuck = self._faults.get(token)
            identity = self._identities.get(token)
            if stuck is None and identity is not None:
                if isinstance(error, WorkerMemory):
                    if not error.args[0].get("identity"):
                        error.args[0]["identity"] = asdict(identity)
                elif isinstance(error, Held):
                    error.fault = error.fault.framed(
                        "pool", "worker", "physical input identity", {"identity": asdict(identity)}
                    )
            return stuck

    def _forget(self, future: Future[Any]) -> None:
        with self._lock:
            token = self._tokens.pop(future, None)
            if token is not None:
                if token in self.watchdog.current:
                    self._retired.add(token)
                else:
                    self._identities.pop(token, None)
                    self._faults.pop(token, None)
                    self._completed.discard(token)

    def kill(self) -> None:
        """Kill every worker group now; queued and running tasks are dropped."""
        executor = self._executor
        if executor is None:
            return
        self.killed = True
        process.kill_groups(list(executor._processes or {}))
        executor.shutdown(wait=False, cancel_futures=True)

    def _signalled(self, number: int, _frame: FrameType | None) -> None:
        self.kill()
        if number == signal.SIGINT:
            raise KeyboardInterrupt
        raise SystemExit(128 + number)

    def _fresh(self, broken: ProcessPoolExecutor | None = None) -> ProcessPoolExecutor:
        """A working executor; with broken, a replacement made since is reused."""
        if broken is not None and self._executor is not broken and self._executor is not None:
            return self._executor
        if self._executor is not None:
            # Recycling after an allocation failure must retain queued siblings.
            self._executor.shutdown()
        self._executor = _executor(
            self.size,
            self.memory_worker_bytes,
            self._work_per_job,
            self.scratch,
            self.cache_memory_bytes,
            self._events,
            None if self._progress_directory is None else str(self._progress_directory),
        )
        return self._executor

    def _submit(self, fn: Callable[[T], R], item: T) -> Future[R]:
        if self._executor is None:
            raise Held(cause_named("pool", "pool: use Pool as a context manager", owner="pool", stage="pool"))
        executor = self._executor
        token: str | None = None
        if fn is _measured and self._events is not None:
            action, argument = cast(tuple[Any, Any], item)
            identity = argument[3][0] if action is _batch and len(argument) == 4 else _identify(action, None, argument)
            token = uuid.uuid4().hex
            with self._lock:
                self.watchdog.queued(token, identity, self._phase)
                self._identities[token] = identity
                self._report(token, "queued", self.watchdog.current[token])
            item = cast(T, (_Submission(action, token, identity), argument))
        try:
            future = executor.submit(fn, item)
        except BrokenProcessPool:
            future = self._fresh(executor).submit(fn, item)
        if token is not None:
            with self._lock:
                self._tokens[future] = token

            def finished(done: Future[Any]) -> None:
                with self._lock:
                    self._completed.add(token)
                if not self._stop.is_set() and self._events is not None:
                    with contextlib.suppress(OSError, ValueError):
                        self._events.put(pickle.dumps((token, "finished", 0, None, "finished")))

            future.add_done_callback(finished)
        return future

    def submit(self, fn: Callable[[T], R], item: T) -> Future[R]:
        """One task; a broken pool is replaced first. The caller retries a crashed task at most once. Its CPU is
        charged to fn (effort); cancelling the returned future cancels the task."""
        from unbake import effort

        self._phase = effort.name_of(fn)
        started = time.monotonic()
        inner = self._submit(_measured, (fn, item))
        outer: Future[R] = _Outer(inner)

        def finished(
            done: Future[tuple[R | None, float, int, dict[str, tuple[int, int]], BaseException | None]],
        ) -> None:
            if done.cancelled():
                self._forget(done)
                outer.cancel()
                return
            error = done.exception()
            stuck = self._failure(done, error)
            if stuck is not None:
                effort.count("worker.stuck", 1, 1)
                self._forget(done)
                outer.set_exception(stuck)
                return
            if error is not None:
                effort.count("worker.crash", 1, 1)
                failed = Held(
                    Fault(
                        cause_named(
                            "worker.crash", f"worker.crash: {effort.name_of(fn)}: {error}", owner="pool", stage="pool"
                        ),
                        (
                            Frame(
                                "context",
                                "pool",
                                "pool",
                                f"worker.crash: {effort.name_of(fn)}: {error}",
                                {
                                    "action": effort.name_of(fn),
                                    "category": "worker-exit",
                                    "cause": type(error).__name__,
                                    "configured_cap_bytes": self.memory_worker_bytes,
                                    "wall_seconds": time.monotonic() - started,
                                    "wall_scope": "submission-to-completion",
                                    "cpu_seconds": None,
                                    "peak_rss_bytes": None,
                                    "counts": None,
                                },
                            ),
                        ),
                    )
                )
                failed.__cause__ = error
                self._failure(done, failed)
                self._forget(done)
                outer.set_exception(failed)
                return
            result, seconds, rss, counts, fault = done.result()
            self._failure(done, fault)
            self._forget(done)
            effort.charge(effort.name_of(fn) + (".failed" if fault is not None else ""), seconds, rss, counts)
            if isinstance(fault, MemoryError):
                effort.count("worker.memory", 1, 1)
            if fault is not None:
                outer.set_exception(fault)
            else:
                outer.set_result(cast(R, result))

        inner.add_done_callback(finished)
        return outer

    def run(self, fn: Callable[..., R], items: Sequence[T], shared: Any = None) -> list[R]:
        """fn over items, batched into jobs by the one sizing rule; results in item order, effort charged to fn.

        With SHARED, it is pickled once to a file and every job names the file: a worker loads it once and calls
        fn(shared, item). The file is removed when run returns."""
        from unbake import effort

        size = min(ITEMS_PER_JOB, width(len(items), self.size))
        if size > self._work_per_job:
            self._work_per_job = size
            self._fresh()
        path: Path | None = None
        if shared is not None:
            if self.scratch is None:
                raise Held(
                    cause_named(
                        "pool.shared",
                        "pool.shared: this pool has no scratch directory for a shared value",
                        owner="pool",
                        stage="pool",
                    )
                )
            self.scratch.mkdir(parents=True, exist_ok=True)
            path = self.scratch / f"shared-{os.getpid()}-{uuid.uuid4().hex}.pickle"
            atomic_files.fresh(path, pickle.dumps(shared, protocol=pickle.HIGHEST_PROTOCOL))
        try:
            jobs = [
                (
                    fn,
                    items[start : start + size],
                    None if path is None else str(path),
                    tuple(_identify(fn, shared, item) for item in items[start : start + size]),
                )
                for start in range(0, len(items), size)
            ]
            return [result for batch in self.map(_batch, jobs, charge=effort.name_of(fn)) for result in batch]
        finally:
            if path is not None:
                path.unlink(missing_ok=True)
                _release_finished_payload(0, None)
                # Notify existing workers; no extra CPU task or worker is started for cleanup.
                processes = None if self._executor is None else self._executor._processes
                for pid in tuple(processes or {}):
                    with contextlib.suppress(ProcessLookupError):
                        os.kill(pid, signal.SIGUSR1)

    def map(self, fn: Callable[[T], R], items: Sequence[T], *, charge: str | None = None) -> Iterator[R]:
        """Refill admitted work on completion; deliver results and failures in input order.

        At most size tasks run at once. Completed later results wait for earlier
        ones; run() already retains the complete ordered result collection.
        """
        from unbake import effort

        name = charge or effort.name_of(fn)
        self._phase = name + ":" + uuid.uuid4().hex
        with self._lock:
            self.watchdog.samples.clear()
        completed: queue.SimpleQueue[tuple[int, Future[Any]]] = queue.SimpleQueue()
        pending: dict[int, tuple[T, Future[Any], int, ProcessPoolExecutor | None]] = {}
        ready: dict[int, tuple[R | None, Exception | None]] = {}
        source = iter(enumerate(items))
        next_result = 0
        refused = False

        def submit(index: int, item: T, attempt: int) -> None:
            future = self._submit(_measured, (fn, item))
            pending[index] = item, future, attempt, self._executor
            future.add_done_callback(lambda done: completed.put((index, done)))

        def fill() -> None:
            if refused:
                return
            while len(pending) < self.size:
                try:
                    index, item = next(source)
                except StopIteration:
                    return
                submit(index, item, 0)

        def retain(failure: TaskFailed, index: int, item: T, result: Any) -> None:
            if fn is _batch and len(cast(Any, item)) == 4:
                failure.completed.extend(zip(cast(Any, item)[3], result, strict=True))
            else:
                failure.completed.append((_identify(fn, None, item), result))

        fill()
        while pending:
            index, future = completed.get()
            item, _, attempt, executor = pending.pop(index)
            # A watchdog is a terminal refusal, not an ordered worker crash.
            # Waiting for an earlier live task could otherwise hide it forever.
            fatal = next(
                (
                    stuck_failure
                    for candidate in (future, *(row[1] for row in pending.values()))
                    if (stuck_failure := self._failure(candidate)) is not None
                ),
                None,
            )
            if fatal is not None:
                effort.count("worker.stuck", 1, 1)
                for ready_index, (ready_result, ready_error) in ready.items():
                    if ready_error is None:
                        retain(fatal, ready_index, items[ready_index], ready_result)
                for completed_index, (completed_item, completed_future) in [
                    (index, (item, future)),
                    *((position, (row[0], row[1])) for position, row in pending.items()),
                ]:
                    if (
                        completed_future.done()
                        and not completed_future.cancelled()
                        and completed_future.exception() is None
                    ):
                        value, seconds, rss, counts, fault = completed_future.result()
                        effort.charge(name + (".failed" if fault is not None else ""), seconds, rss, counts)
                        if fault is None:
                            retain(fatal, completed_index, completed_item, value)
                    self._forget(completed_future)
                raise fatal
            try:
                stuck = self._failure(future)
                if stuck is not None:
                    effort.count("worker.stuck", 1, 1)
                    raise stuck
                result, seconds, rss, counts, fault = future.result()
                self._failure(future, fault)
                effort.charge(name + (".failed" if fault is not None else ""), seconds, rss, counts)
                if fault is not None:
                    raise fault
            except (BrokenProcessPool, MemoryError) as error:
                self._failure(future, error)
                failure = "worker.memory" if isinstance(error, MemoryError) else "worker.crash"
                effort.count(failure, 1, 1)
                if attempt or isinstance(error, MemoryError):
                    diagnostic = (
                        dict(error.args[0])
                        if isinstance(error, WorkerMemory)
                        else {
                            "action": name,
                            "category": "worker-exit",
                            "cause": type(error).__name__,
                            "cpu_seconds": None,
                            "peak_rss_bytes": None,
                            "counts": None,
                        }
                    )
                    diagnostic["configured_cap_bytes"] = self.memory_worker_bytes
                    terminal = TaskFailed(failure, diagnostic)
                    terminal.__cause__ = error
                    ready[index] = None, terminal
                    refused = True
                else:
                    effort.count("worker.retry", 1, 1)
                    # Only failed tasks retry. Successful siblings keep their
                    # outputs, and failures from the same executor share its replacement.
                    self._fresh(executor)
                    submit(index, item, 1)
            except Exception as error:
                self._failure(future, error)
                ready[index] = None, error
                refused = True
            else:
                ready[index] = result, None
            self._forget(future)
            fill()
            while next_result in ready:
                result, refused_error = ready.pop(next_result)
                if refused_error is not None:
                    raise refused_error
                yield cast(R, result)
                next_result += 1


_shared: Pool | None = None


@contextlib.contextmanager
def sharing(pool: Pool) -> Iterator[None]:
    """While open, run() uses this open pool, so its workers are never doubled."""
    global _shared
    previous, _shared = _shared, pool
    try:
        yield
    finally:
        _shared = previous


@contextlib.contextmanager
def session(host: Host) -> Iterator[None]:
    """One pool for everything run inside: the open shared pool if there is one, else a pool opened here. Without
    it every run() starts its own workers, and a step of several fan-outs starts them again for each."""
    if _shared is not None:
        yield
        return
    with Pool.from_host(host) as opened, sharing(opened):
        yield


def cpu(fn: Callable[..., R]) -> Callable[..., R]:
    """Require worker admission even for a single CPU item; retain its importable identity."""
    fn._pool_worker = True  # type: ignore[attr-defined]
    return fn


def in_worker() -> bool:
    """True while a pool worker runs a job: work asked for there runs in that worker, never in a nested pool."""
    return _token is not None


def run(host: Host, fn: Callable[..., R], items: Sequence[T], shared: Any = None) -> list[R]:
    """Run fn over items in the shared pool, else the host's pool.
    CPU-marked functions run in workers even for a single item.
    With SHARED, fn takes it first: fn(shared, item)."""
    if in_worker() or (len(items) < 2 and not getattr(fn, "_pool_worker", False)):
        return [fn(item) if shared is None else fn(shared, item) for item in items]
    if not items:
        return []
    if _shared is not None:
        return _shared.run(fn, items, shared)
    with Pool.from_host(host) as pool:
        return pool.run(fn, items, shared)


def workers(host: Host) -> int:
    """How many workers the host's pool runs."""
    return admitted(
        min(host.workers, host.cores), host.memory_total_bytes, host.memory_parent_bytes, host.memory_worker_bytes
    )


def describe(host: Host) -> dict[str, Any]:
    return {"workers": workers(host)}
