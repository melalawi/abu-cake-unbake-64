"""Tasks: a label, a total when one is known, a count done, and the task it runs inside.

task() opens one around a block and tells the renderer chosen by output.start (nothing, before it is chosen)
when it starts, advances and ends; the done line says how long it took and how many cores it kept busy."""

from __future__ import annotations

import contextlib
import threading
import time
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from typing import Protocol, TypeVar

from unbake import effort

T = TypeVar("T")


@dataclass
class Task:
    label: str
    total: int | None = None
    done: int = 0
    started: float = field(default_factory=time.monotonic)
    parent: Task | None = None
    # Words the owner adds to the done line (the types step: what changed).
    note: str = ""
    # What this task's finished children account for (effort.NUMERIC), so the rest is its own work.
    kids: dict[str, float] = field(default_factory=dict)
    kid_count: int = 0

    def advance(self, n: int = 1) -> None:
        with _lock:
            self.done += n
        if _renderer is not None:
            _renderer.progress(self)

    def set_total(self, n: int) -> None:
        with _lock:
            self.total = n
        if _renderer is not None:
            _renderer.progress(self)

    @property
    def path(self) -> str:
        return self.label if self.parent is None else f"{self.parent.path} > {self.label}"

    @property
    def depth(self) -> int:
        return 0 if self.parent is None else self.parent.depth + 1


class Renderer(Protocol):
    def start(self, task: Task) -> None: ...
    def progress(self, task: Task) -> None: ...
    def done(self, task: Task, seconds: float, cores: float, extra: str) -> None: ...
    def line(self, text: str, depth: int) -> None: ...
    def verdict(self, kind: str, text: str, depth: int) -> None: ...


_lock = threading.Lock()
_stack: list[Task] = []
_renderer: Renderer | None = None


def bind(renderer: Renderer | None) -> None:
    global _renderer
    _renderer = renderer


def current() -> Task | None:
    with _lock:
        return _stack[-1] if _stack else None


def depth() -> int:
    """How deep a plain line sits: under the current task, else at the left edge."""
    open_task = current()
    return 0 if open_task is None else open_task.depth + 1


def _cache(spent: effort.Effort) -> str:
    hits = total = 0
    for name, (done, count) in spent.counts.items():
        if name.startswith("cache."):
            hits += done
            total += count
    return f"; {hits} of {total} reused from the cache" if total else ""


@contextlib.contextmanager
def task(label: str, total: int | None = None) -> Iterator[Task]:
    with _lock:
        opened = Task(label, total, parent=_stack[-1] if _stack else None)
        _stack.append(opened)
    mark = effort.mark()
    if _renderer is not None:
        _renderer.start(opened)
    try:
        yield opened
    finally:
        with _lock:
            _stack.remove(opened)
        spent = effort.since(mark)
        path = opened.path
        whole = effort.stage_detail(path, spent)
        if not spent.pools:
            whole["items"] = opened.total or 0
        effort.record_detail(path, whole)
        if effort.parent_only(path, opened.total or 0, bool(spent.pool), spent.wall, spent.cpu):
            effort.record_stage(f"{effort.PARENT_ONLY}{path} ({opened.total or 0} items)", spent.wall, spent.cpu)
        if opened.kid_count:
            own = effort.own_detail(f"{path} (own)", whole, opened.kids)
            own_cpu = effort.record_detail(f"{path} (own)", own)
            own_wall, own_pool = float(own["wall_seconds"]), float(own["worker_cpu_seconds"])  # type: ignore[arg-type]
            if effort.parent_only(f"{path} (own)", 0, own_pool > 0, own_wall, own_cpu):
                effort.record_stage(f"{effort.PARENT_ONLY}{path} (own)", own_wall, own_cpu)
        if opened.parent is not None:
            with _lock:
                opened.parent.kid_count += 1
                for key, value in effort.numbers(whole).items():
                    opened.parent.kids[key] = opened.parent.kids.get(key, 0.0) + value
        if _renderer is not None:
            cores = spent.cpu / spent.wall if spent.wall > 0 else 0.0
            _renderer.done(opened, spent.wall, cores, _cache(spent) + opened.note)


def each(items: Sequence[T], label: str) -> Iterator[T]:
    with task(label, len(items)) as opened:
        for item in items:
            yield item
            opened.advance()
