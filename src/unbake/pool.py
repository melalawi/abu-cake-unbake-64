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
import multiprocessing
import multiprocessing.connection
import os
import pickle
import queue
import resource
import select
import shutil
import signal
import sys
import tempfile
import threading
import time
import traceback
import uuid
from collections.abc import Callable, Iterator, Sequence
from concurrent.futures import Future, ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from dataclasses import dataclass
from pathlib import Path
from types import FrameType
from typing import Any, TypeVar, cast

from unbake import atomic as atomic_files
from unbake.config import Held, Host
from unbake.process import temporary_environment

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
        super().__init__("pool", f"{key}: {source} {functions} ({versions}): {where}; retry failed", fault=fault)
        self.failure = key


def admitted(workers: int, memory_total_bytes: int, memory_parent_bytes: int, memory_worker_bytes: int) -> int:
    """How many workers fit: at most `workers`, and their caps fit within total - parent."""
    if memory_worker_bytes <= 0 or memory_total_bytes <= memory_parent_bytes:
        raise Held("pool", "pool.memory: worker cap and total-parent budget must be positive")
    return max(1, min(workers, (memory_total_bytes - memory_parent_bytes) // memory_worker_bytes))


def owner(server: int) -> int:
    """The process that started the fork server `server`: the one that owns the pool."""
    stat = Path(f"/proc/{server}/stat").read_text()
    return int(stat.rsplit(")", 1)[1].split()[1])


def _die_with_owner() -> None:
    """Wait on the owner's pidfd in a thread; when it exits, kill the fork server and this group."""
    server = os.getppid()
    try:
        descriptor: int | None = os.pidfd_open(owner(server))
    except (ProcessLookupError, FileNotFoundError):
        descriptor = None
    threading.Thread(target=_orphaned, args=(descriptor, server), name="owner", daemon=True).start()


def _orphaned(descriptor: int | None, server: int) -> None:
    """When the owner exits, kill the fork server and this worker's group. A stopped owner often takes the server
    down first: its absence must never spare the group (orphan workers kept running and holding memory)."""
    if descriptor is not None:
        select.select([descriptor], [], [])
    with contextlib.suppress(ProcessLookupError):
        os.kill(server, signal.SIGKILL)
    with contextlib.suppress(ProcessLookupError):
        os.killpg(0, signal.SIGKILL)


def _cap(memory_worker_bytes: int, directory: str, cache_memory_bytes: int | None) -> None:
    """Worker start: lead a new process group, die with the pool's owner, cap the data segment."""
    from unbake import cache

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


def kill_groups(pids: Sequence[int]) -> None:
    """SIGKILL each worker's process group (the worker itself when its group does not exist yet)."""
    for pid in pids:
        try:
            os.killpg(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            with contextlib.suppress(ProcessLookupError):
                os.kill(pid, signal.SIGKILL)


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
            "pool",
            f"pool.socket: cache.machine_root {root} is too long for the worker socket; configure a shorter path",
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
) -> ProcessPoolExecutor:
    if scratch is None:
        raise Held("pool", "pool.scratch: cache.machine_root is required for worker transport")
    directory = _socket_directory(scratch)
    return ProcessPoolExecutor(
        max_workers=size,
        mp_context=multiprocessing.get_context("forkserver"),
        initializer=_cap,
        initargs=(memory_worker_bytes, directory, cache_memory_bytes),
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
            "pool",
            f"{effort.name_of(fn)}: {type(error).__name__} at {_where(error)}: {error}",
            fault={
                "action": effort.name_of(fn),
                "category": "python",
                "cause": type(error).__name__,
                "where": _where(error),
            },
        ) from error


def _measured(
    task: tuple[Callable[[T], R], T],
) -> tuple[R | None, float, int, dict[str, tuple[int, int]], BaseException | None]:
    """Return effort at the action boundary, even when its result is a fault."""
    from unbake import effort

    fn, item = task
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
        from unbake.process import fault

        chain = fault(error)
        error.fault = {**(error.fault or {}), **chain, **measured}
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


def _batch(job: tuple[Callable[..., R], Sequence[T], str | None]) -> list[R]:
    fn, chunk, path = job
    if path is None:
        return [_named(fn, item) for item in chunk]
    shared = _shared_value(path)
    return [_named(fn, shared, item) for item in chunk]


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
        self._executor = _executor(
            self.size, self.memory_worker_bytes, self._work_per_job, self.scratch, self.cache_memory_bytes
        )
        if threading.current_thread() is threading.main_thread():
            self._handlers = {number: signal.signal(number, self._signalled) for number in SIGNALS}
        atexit.register(self.kill)
        return self

    def __exit__(self, kind: type[BaseException] | None, *exc: object) -> None:
        try:
            if kind is not None or self.killed:
                self.kill()
            elif self._executor is not None:
                self._executor.shutdown(cancel_futures=True)
        finally:
            self._executor = None
            atexit.unregister(self.kill)
            for number, previous in self._handlers.items():
                signal.signal(number, previous)
            self._handlers = {}

    def kill(self) -> None:
        """Kill every worker group now; queued and running tasks are dropped."""
        executor = self._executor
        if executor is None:
            return
        self.killed = True
        kill_groups(list(executor._processes or {}))
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
            self.size, self.memory_worker_bytes, self._work_per_job, self.scratch, self.cache_memory_bytes
        )
        return self._executor

    def _submit(self, fn: Callable[[T], R], item: T) -> Future[R]:
        if self._executor is None:
            raise Held("pool", "pool: use Pool as a context manager")
        executor = self._executor
        try:
            return executor.submit(fn, item)
        except BrokenProcessPool:
            return self._fresh(executor).submit(fn, item)

    def submit(self, fn: Callable[[T], R], item: T) -> Future[R]:
        """One task; a broken pool is replaced first. The caller retries a crashed task at most once. Its CPU is
        charged to fn (effort); cancelling the returned future cancels the task."""
        from unbake import effort

        started = time.monotonic()
        inner = self._submit(_measured, (fn, item))
        outer: Future[R] = _Outer(inner)

        def finished(
            done: Future[tuple[R | None, float, int, dict[str, tuple[int, int]], BaseException | None]],
        ) -> None:
            if done.cancelled():
                outer.cancel()
                return
            error = done.exception()
            if error is not None:
                effort.count("worker.crash", 1, 1)
                failed = Held(
                    "pool",
                    f"worker.crash: {effort.name_of(fn)}: {error}",
                    fault={
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
                )
                failed.__cause__ = error
                outer.set_exception(failed)
                return
            result, seconds, rss, counts, fault = done.result()
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
                raise Held("pool", "pool.shared: this pool has no scratch directory for a shared value")
            self.scratch.mkdir(parents=True, exist_ok=True)
            path = self.scratch / f"shared-{os.getpid()}-{uuid.uuid4().hex}.pickle"
            atomic_files.fresh(path, pickle.dumps(shared, protocol=pickle.HIGHEST_PROTOCOL))
        try:
            jobs = [
                (fn, items[start : start + size], None if path is None else str(path))
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

        fill()
        while pending:
            index, future = completed.get()
            item, _, attempt, executor = pending.pop(index)
            try:
                result, seconds, rss, counts, fault = future.result()
                effort.charge(name + (".failed" if fault is not None else ""), seconds, rss, counts)
                if fault is not None:
                    raise fault
            except (BrokenProcessPool, MemoryError) as error:
                failure = "worker.memory" if isinstance(error, MemoryError) else "worker.crash"
                effort.count(failure, 1, 1)
                if attempt:
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
                ready[index] = None, error
                refused = True
            else:
                ready[index] = result, None
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


def run(host: Host, fn: Callable[..., R], items: Sequence[T], shared: Any = None) -> list[R]:
    """Run fn over items in the shared pool, else the host's pool.
    CPU-marked functions run in workers even for a single item.
    With SHARED, fn takes it first: fn(shared, item)."""
    if len(items) < 2 and not getattr(fn, "_pool_worker", False):
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
