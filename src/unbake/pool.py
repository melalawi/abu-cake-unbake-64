"""The worker pool for CPU work: forkserver processes, memory admission, recycling, one retry.

Workers start from a fork server created before any project data is loaded, so they start small.
Each worker caps its own data segment at memory_worker_bytes (RLIMIT_DATA, inherited by the tools it
runs). A task whose worker dies or runs out of memory is retried once in a fresh worker; a second
failure raises TaskFailed naming worker.crash or worker.memory.
"""

from __future__ import annotations

import multiprocessing
import resource
from collections import deque
from collections.abc import Callable, Iterator, Sequence
from concurrent.futures import Future, ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from typing import Any, TypeVar

from unbake.config import Held, Host

T = TypeVar("T")
R = TypeVar("R")

RECYCLE_AFTER = 64


class TaskFailed(Held):
    def __init__(self, key: str, item: object) -> None:
        super().__init__("pool", f"{key}: task {item!r} failed twice")
        self.failure = key
        self.item = item


def admitted(workers: int, memory_total_bytes: int, memory_parent_bytes: int, memory_worker_bytes: int) -> int:
    """How many workers fit: at most `workers`, and their caps fit within total - parent."""
    if memory_worker_bytes <= 0 or memory_total_bytes <= memory_parent_bytes:
        raise Held("pool", "pool.memory: worker cap and total-parent budget must be positive")
    return max(1, min(workers, (memory_total_bytes - memory_parent_bytes) // memory_worker_bytes))


def _cap(memory_worker_bytes: int) -> None:
    resource.setrlimit(resource.RLIMIT_DATA, (memory_worker_bytes, memory_worker_bytes))


def _executor(size: int, memory_worker_bytes: int) -> ProcessPoolExecutor:
    return ProcessPoolExecutor(
        max_workers=size,
        mp_context=multiprocessing.get_context("forkserver"),
        initializer=_cap,
        initargs=(memory_worker_bytes,),
        max_tasks_per_child=RECYCLE_AFTER,
    )


class Pool:
    def __init__(self, workers: int, memory_total_bytes: int, memory_parent_bytes: int, memory_worker_bytes: int):
        self.size = admitted(workers, memory_total_bytes, memory_parent_bytes, memory_worker_bytes)
        self.memory_worker_bytes = memory_worker_bytes
        self._executor: ProcessPoolExecutor | None = None

    @classmethod
    def from_host(cls, host: Host) -> Pool:
        return cls(host.workers, host.memory_total_bytes, host.memory_parent_bytes, host.memory_worker_bytes)

    def __enter__(self) -> Pool:
        self._executor = _executor(self.size, self.memory_worker_bytes)
        return self

    def __exit__(self, *exc: object) -> None:
        if self._executor is not None:
            self._executor.shutdown(cancel_futures=True)
            self._executor = None

    def _fresh(self) -> ProcessPoolExecutor:
        if self._executor is not None:
            self._executor.shutdown(cancel_futures=True)
        self._executor = _executor(self.size, self.memory_worker_bytes)
        return self._executor

    def _submit(self, fn: Callable[[T], R], item: T) -> Future[R]:
        if self._executor is None:
            raise Held("pool", "pool: use Pool as a context manager")
        try:
            return self._executor.submit(fn, item)
        except BrokenProcessPool:
            return self._fresh().submit(fn, item)

    def map(self, fn: Callable[[T], R], items: Sequence[T]) -> Iterator[R]:
        """Results in item order; at most `size` tasks in flight."""
        pending: deque[tuple[T, Future[R], int]] = deque()
        source = iter(items)
        for item in source:
            pending.append((item, self._submit(fn, item), 0))
            if len(pending) >= self.size:
                break
        while pending:
            item, future, attempt = pending.popleft()
            try:
                result = future.result()
            except (BrokenProcessPool, MemoryError) as error:
                failure = "worker.memory" if isinstance(error, MemoryError) else "worker.crash"
                if attempt:
                    raise TaskFailed(failure, item) from error
                if isinstance(error, BrokenProcessPool):
                    self._fresh()
                    pending = deque((i, self._submit(fn, i), a) for i, _, a in pending)
                pending.appendleft((item, self._submit(fn, item), 1))
                continue
            yield result
            for item in source:
                pending.append((item, self._submit(fn, item), 0))
                break


def run(host: Host, fn: Callable[[T], R], items: Sequence[T]) -> list[R]:
    """Run fn over items in the host's pool; a single item runs in this process."""
    if len(items) < 2:
        return [fn(item) for item in items]
    with Pool.from_host(host) as pool:
        return list(pool.map(fn, items))


def describe(host: Host) -> dict[str, Any]:
    return {"workers": admitted(host.workers, host.memory_total_bytes, host.memory_parent_bytes, host.memory_worker_bytes)}
