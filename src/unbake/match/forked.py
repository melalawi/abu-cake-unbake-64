"""Order-preserving process fan-out for CPU-bound batch work."""

from __future__ import annotations

import multiprocessing
import threading
from collections.abc import Callable, Generator, Sequence
from concurrent.futures import ProcessPoolExecutor
from typing import Any, TypeVar

from unbake.match import reporting

S = TypeVar("S")
T = TypeVar("T")
R = TypeVar("R")

_work: tuple[Callable[[Any, Any], Any], Any] | None = None


def ordered(work: Callable[[S, T], R], shared: S, items: Sequence[T], cores: int) -> Generator[tuple[list[str], R]]:
    """Yield work(shared, item) with the receipts it learned, in item order.

    Workers are forked, so they inherit shared and every input this process
    already parsed instead of reading them again. Forking beside live threads
    is unsafe, so a process with other threads runs the same work in place.
    """
    global _work
    if cores < 2 or len(items) < 2 or threading.active_count() > 1:
        yield from (_run(work, shared, item) for item in items)
        return
    _work = work, shared
    try:
        context = multiprocessing.get_context("fork")
        with ProcessPoolExecutor(max_workers=min(cores, len(items)), mp_context=context) as pool:
            try:
                yield from pool.map(_call, items, chunksize=max(1, len(items) // (cores * 4)))
            finally:
                pool.shutdown(cancel_futures=True)
    finally:
        _work = None


def _call(item: Any) -> tuple[list[str], Any]:
    assert _work is not None
    return _run(_work[0], _work[1], item)


def _run(work: Callable[[S, T], R], shared: S, item: T) -> tuple[list[str], R]:
    with reporting.captured() as lines:
        return lines, work(shared, item)
