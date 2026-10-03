"""Order-preserving process fan-out for CPU-bound batch work."""

from __future__ import annotations

import multiprocessing
import pickle
import tempfile
import threading
from collections.abc import Callable, Generator, Iterator, Sequence
from concurrent.futures import ProcessPoolExecutor
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Any, TypeVar

from unbake.match import reporting

S = TypeVar("S")
T = TypeVar("T")
R = TypeVar("R")

_work: tuple[Callable[[Any, Any], Any], Any] | None = None


_pool: ContextVar[tuple[ProcessPoolExecutor, Path] | None] = ContextVar("batch_pool", default=None)
_snapshot: tuple[str, tuple[Callable[[Any, Any], Any], Any]] | None = None


@contextmanager
def session(cores: int, jobs: int) -> Iterator[None]:
    """Own one lazy worker pool for a batch; each phase transfers one snapshot.

    Immutable phase snapshots avoid inheriting stale parent state between phases.
    A worker deserializes each shared input once, rather than once per source.
    """
    if _pool.get() is not None or cores < 2 or jobs < 2 or threading.active_count() > 1:
        yield
        return
    with (
        tempfile.TemporaryDirectory(prefix="submit-workers-") as temporary,
        ProcessPoolExecutor(max_workers=min(cores, jobs), mp_context=multiprocessing.get_context("fork")) as pool,
    ):
        token = _pool.set((pool, Path(temporary)))
        try:
            yield
        finally:
            _pool.reset(token)


def _phase(item: tuple[str, Any]) -> tuple[list[str], Any]:
    global _snapshot
    name, value = item
    if _snapshot is None or _snapshot[0] != name:
        with open(name, "rb") as stream:
            _snapshot = name, pickle.load(stream)
    work, shared = _snapshot[1]
    return _run(work, shared, value)


def ordered(work: Callable[[S, T], R], shared: S, items: Sequence[T], cores: int) -> Generator[tuple[list[str], R]]:
    """Yield work(shared, item) with the receipts it learned, in item order.

    Workers are forked, so they inherit shared and every input this process
    already parsed instead of reading them again. Forking beside live threads
    is unsafe, so a process with other threads runs the same work in place.
    """
    global _work
    active = _pool.get()
    if cores < 2 or len(items) < 2 or (active is None and threading.active_count() > 1):
        yield from (_run(work, shared, item) for item in items)
        return
    if active is not None:
        pool, directory = active
        with tempfile.NamedTemporaryFile(dir=directory, suffix=".pickle", delete=False) as stream:
            pickle.dump((work, shared), stream, protocol=pickle.HIGHEST_PROTOCOL)
            name = stream.name
        yield from pool.map(_phase, ((name, item) for item in items), chunksize=max(1, len(items) // (cores * 4)))
        return
    _work = work, shared
    try:
        context = multiprocessing.get_context("fork")
        with ProcessPoolExecutor(max_workers=min(cores, len(items)), mp_context=context) as pool:
            yield from pool.map(_call, items, chunksize=max(1, len(items) // (cores * 4)))
    finally:
        _work = None


def _call(item: Any) -> tuple[list[str], Any]:
    assert _work is not None
    return _run(_work[0], _work[1], item)


def _run(work: Callable[[S, T], R], shared: S, item: T) -> tuple[list[str], R]:
    with reporting.captured() as lines:
        return lines, work(shared, item)
