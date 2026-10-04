"""Order-preserving process fan-out for CPU-bound batch work."""

from __future__ import annotations

import gc
import multiprocessing
import pickle
import tempfile
import threading
from collections import deque
from collections.abc import Callable, Generator, Iterator, Sequence
from concurrent.futures import Executor, Future, ProcessPoolExecutor, wait
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Any, TypeVar

from unbake.match import reporting

S = TypeVar("S")
T = TypeVar("T")
R = TypeVar("R")

MAX_WORKERS = 2

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
        ProcessPoolExecutor(
            max_workers=min(cores, jobs, MAX_WORKERS), mp_context=multiprocessing.get_context("fork")
        ) as pool,
    ):
        token = _pool.set((pool, Path(temporary)))
        try:
            yield
        finally:
            _pool.reset(token)


def finish() -> None:
    """Release idle workers and their shared snapshots before publication feedback."""
    active = _pool.get()
    if active is not None:
        pool, _ = active
        _pool.set(None)
        pool.shutdown(cancel_futures=True)
    release()


def _phase(item: tuple[str, Any]) -> tuple[list[str], Any]:
    global _snapshot
    name, value = item
    if _snapshot is None or _snapshot[0] != name:
        _snapshot = None
        release()
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
        try:
            yield from _bounded(pool, _phase, [(name, item) for item in items], min(cores, MAX_WORKERS))
        finally:
            Path(name).unlink(missing_ok=True)
        return
    _work = work, shared
    try:
        context = multiprocessing.get_context("fork")
        with ProcessPoolExecutor(max_workers=min(cores, len(items), MAX_WORKERS), mp_context=context) as pool:
            yield from _bounded(pool, _call, items, min(cores, MAX_WORKERS))
    finally:
        _work = None


def _bounded(pool: Executor, work: Callable[[T], R], items: Sequence[T], workers: int) -> Generator[R]:
    """Retain at most one pending result per worker, including on early close."""
    pending: deque[Future[R]] = deque()
    source = iter(items)
    try:
        for _ in range(workers):
            try:
                item = next(source)
            except StopIteration:
                break
            pending.append(pool.submit(work, item))
        while pending:
            result = pending.popleft().result()
            yield result
            del result
            try:
                item = next(source)
            except StopIteration:
                continue
            pending.append(pool.submit(work, item))
    finally:
        running = [future for future in pending if not future.cancel()]
        wait(running)


def release(*, shared: bool = False) -> None:
    """Drop disposable parse state at a candidate boundary; disk artifacts survive."""
    from unbake.match import type_rewrite
    from unbake.project import cache

    if shared:
        for key in list(cache._parsed):
            if key[0] not in {"split.layout", "split.symbols"}:
                del cache._parsed[key]
        while len(cache._parsed) > 16:
            del cache._parsed[next(iter(cache._parsed))]
    else:
        cache._parsed.clear()
    if shared:
        # Header analyses are content keyed and small per file. Larger graphs
        # retain only the current context, never a history of candidate parses.
        reusable = {
            "headers.declarations",
            "headers.aliases",
            "headers.context",
            "imports.providers",
            "rewrite.namespaces",
            "rewrite.plans",
            "declaration.evidence",
            "parsed.split.functions",
            "split.aliases",
        }
        for kind in list(cache._remembered):
            if kind not in reusable:
                del cache._remembered[kind]
            elif kind not in {"headers.declarations", "headers.aliases"}:
                values = cache._remembered[kind]
                while len(values) > (8 if kind in {"parsed.split.functions", "split.aliases"} else 1):
                    values.popitem(last=False)
    else:
        for kind in list(cache._remembered):
            if kind not in {"headers.declarations", "headers.aliases"}:
                del cache._remembered[kind]
        type_rewrite._context.cache_clear()
    gc.collect()


def _call(item: Any) -> tuple[list[str], Any]:
    assert _work is not None
    return _run(_work[0], _work[1], item)


def _run(work: Callable[[S, T], R], shared: S, item: T) -> tuple[list[str], R]:
    try:
        with reporting.captured() as lines:
            return lines, work(shared, item)
    finally:
        release(shared=True)
