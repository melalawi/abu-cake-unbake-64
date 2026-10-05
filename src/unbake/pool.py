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
import os
import resource
import select
import shutil
import signal
import sys
import tempfile
import threading
from collections import deque
from collections.abc import Callable, Iterator, Sequence
from concurrent.futures import Future, ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from pathlib import Path
from types import FrameType
from typing import Any, TypeVar

from unbake.config import Held, Host

T = TypeVar("T")
R = TypeVar("R")

RECYCLE_AFTER = 64
ITEM_SHOWN = 300
SIGNALS = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)


class TaskFailed(Held):
    def __init__(self, key: str, item: object) -> None:
        text = repr(item)
        shown = text if len(text) <= ITEM_SHOWN else text[:ITEM_SHOWN] + f"... ({len(text)} characters)"
        super().__init__("pool", f"{key}: task {shown} failed twice")
        self.failure = key
        self.item = item


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

    def orphaned(descriptor: int | None) -> None:
        if descriptor is not None:
            select.select([descriptor], [], [])
        os.kill(server, signal.SIGKILL)
        os.killpg(0, signal.SIGKILL)

    try:
        descriptor: int | None = os.pidfd_open(owner(server))
    except (ProcessLookupError, FileNotFoundError):
        descriptor = None
    threading.Thread(target=orphaned, args=(descriptor,), name="owner", daemon=True).start()


def _cap(memory_worker_bytes: int) -> None:
    """Worker start: lead a new process group, die with the pool's owner, cap the data segment."""
    os.setpgid(0, 0)
    if sys.platform == "linux":
        _die_with_owner()
    resource.setrlimit(resource.RLIMIT_DATA, (memory_worker_bytes, memory_worker_bytes))


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


def _socket_directory() -> None:
    """Put multiprocessing's private dir under XDG_RUNTIME_DIR when TMPDIR is too long for the socket path."""
    config = multiprocessing.process.current_process()._config  # type: ignore[attr-defined]
    if "tempdir" in config or len(tempfile.gettempdir()) <= _SOCKET_ROOM:
        return
    runtime = os.environ.get("XDG_RUNTIME_DIR", "")
    if not runtime or len(runtime) > _SOCKET_ROOM:
        raise Held(
            "pool",
            f"pool.socket: TMPDIR {tempfile.gettempdir()} is too long for the worker socket; "
            "set XDG_RUNTIME_DIR or a shorter TMPDIR",
        )
    directory = tempfile.mkdtemp(prefix="pymp-", dir=runtime)
    atexit.register(shutil.rmtree, directory, ignore_errors=True)
    config["tempdir"] = directory


def _executor(size: int, memory_worker_bytes: int) -> ProcessPoolExecutor:
    _socket_directory()
    return ProcessPoolExecutor(
        max_workers=size,
        mp_context=multiprocessing.get_context("forkserver"),
        initializer=_cap,
        initargs=(memory_worker_bytes,),
        max_tasks_per_child=RECYCLE_AFTER,
    )


def _cpu() -> float:
    own, children = resource.getrusage(resource.RUSAGE_SELF), resource.getrusage(resource.RUSAGE_CHILDREN)
    return own.ru_utime + own.ru_stime + children.ru_utime + children.ru_stime


def _measured(task: tuple[Callable[[T], R], T]) -> tuple[R, float]:
    """Worker body: the task's result and the CPU it and the tools it ran spent (effort ledger)."""
    fn, item = task
    start = _cpu()
    result = fn(item)
    return result, _cpu() - start


class Pool:
    def __init__(self, workers: int, memory_total_bytes: int, memory_parent_bytes: int, memory_worker_bytes: int):
        self.size = admitted(workers, memory_total_bytes, memory_parent_bytes, memory_worker_bytes)
        self.memory_worker_bytes = memory_worker_bytes
        self._executor: ProcessPoolExecutor | None = None
        self._handlers: dict[int, Any] = {}
        self.killed = False

    @classmethod
    def from_host(cls, host: Host) -> Pool:
        return cls(host.workers, host.memory_total_bytes, host.memory_parent_bytes, host.memory_worker_bytes)

    def __enter__(self) -> Pool:
        if threading.current_thread() is threading.main_thread():
            self._handlers = {number: signal.signal(number, self._signalled) for number in SIGNALS}
        atexit.register(self.kill)
        self._executor = _executor(self.size, self.memory_worker_bytes)
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
            self._executor.shutdown(cancel_futures=True)
        self._executor = _executor(self.size, self.memory_worker_bytes)
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

        inner = self._submit(_measured, (fn, item))
        outer: Future[R] = Future()

        def finished(done: Future[tuple[R, float]]) -> None:
            if done.cancelled():
                outer.cancel()
                return
            error = done.exception()
            if error is not None:
                outer.set_exception(error)
                return
            result, seconds = done.result()
            effort.charge(effort.name_of(fn), seconds)
            outer.set_result(result)

        outer.add_done_callback(lambda done: inner.cancel() if done.cancelled() else None)
        inner.add_done_callback(finished)
        return outer

    def map(self, fn: Callable[[T], R], items: Sequence[T]) -> Iterator[R]:
        """Results in item order; at most `size` tasks in flight. Each task's CPU is charged to fn (effort)."""
        from unbake import effort

        name = effort.name_of(fn)

        def submit(item: T) -> Future[tuple[R, float]]:
            return self._submit(_measured, (fn, item))

        pending: deque[tuple[T, Future[tuple[R, float]], int]] = deque()
        source = iter(items)
        for item in source:
            pending.append((item, submit(item), 0))
            if len(pending) >= self.size:
                break
        while pending:
            item, future, attempt = pending.popleft()
            try:
                result, seconds = future.result()
            except (BrokenProcessPool, MemoryError) as error:
                failure = "worker.memory" if isinstance(error, MemoryError) else "worker.crash"
                if attempt:
                    raise TaskFailed(failure, item) from error
                if isinstance(error, BrokenProcessPool):
                    self._fresh()
                    pending = deque((i, submit(i), a) for i, _, a in pending)
                pending.appendleft((item, submit(item), 1))
                continue
            effort.charge(name, seconds)
            yield result
            for item in source:
                pending.append((item, submit(item), 0))
                break


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


def run(host: Host, fn: Callable[[T], R], items: Sequence[T]) -> list[R]:
    """Run fn over items in the shared pool, else the host's pool; a single item runs in this process."""
    if len(items) < 2:
        return [fn(item) for item in items]
    if _shared is not None:
        return list(_shared.map(fn, items))
    with Pool.from_host(host) as pool:
        return list(pool.map(fn, items))


def describe(host: Host) -> dict[str, Any]:
    return {
        "workers": admitted(host.workers, host.memory_total_bytes, host.memory_parent_bytes, host.memory_worker_bytes)
    }
