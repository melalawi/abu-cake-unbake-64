"""CPU effort of one command: wall time, this process, the tools it ran and waited for, and pool work by function.

The pool charges each task's worker CPU to its function (pool.run); a command reports the totals on stderr when
it ends, and each step reports its own share."""

from __future__ import annotations

import resource
import threading
import time
from dataclasses import dataclass

TOP = 5

_lock = threading.Lock()
# function name -> [worker CPU seconds, tasks]
_ledger: dict[str, list[float]] = {}


def charge(name: str, seconds: float) -> None:
    with _lock:
        row = _ledger.setdefault(name, [0.0, 0])
        row[0] += seconds
        row[1] += 1


def name_of(fn: object) -> str:
    module = getattr(fn, "__module__", "") or ""
    return f"{module.removeprefix('unbake.')}.{getattr(fn, '__qualname__', repr(fn))}"


@dataclass(frozen=True)
class Mark:
    wall: float
    main: float
    tools: float
    pool: dict[str, tuple[float, int]]


def mark() -> Mark:
    own = resource.getrusage(resource.RUSAGE_SELF)
    children = resource.getrusage(resource.RUSAGE_CHILDREN)
    with _lock:
        pool = {name: (row[0], int(row[1])) for name, row in _ledger.items()}
    return Mark(time.monotonic(), own.ru_utime + own.ru_stime, children.ru_utime + children.ru_stime, pool)


@dataclass(frozen=True)
class Effort:
    wall: float
    main: float
    tools: float
    pool: dict[str, tuple[float, int]]

    @property
    def cpu(self) -> float:
        return self.main + self.tools + sum(seconds for seconds, _ in self.pool.values())

    def line(self) -> str:
        percent = 100 * self.cpu / self.wall if self.wall > 0 else 0.0
        pooled = sum(seconds for seconds, _ in self.pool.values())
        text = (
            f"effort: {self.wall:.1f} s wall, {self.cpu:.1f} cpu-s ({percent:.0f}%): main {self.main:.1f}, "
            f"tools {self.tools:.1f}, pool {pooled:.1f}"
        )
        top = sorted(self.pool.items(), key=lambda item: -item[1][0])[:TOP]
        if top:
            text += " [" + ", ".join(f"{name} {seconds:.1f} x{tasks}" for name, (seconds, tasks) in top) + "]"
        return text

    def document(self) -> dict[str, float]:
        return {"wall_seconds": round(self.wall, 3), "cpu_seconds": round(self.cpu, 3)}


def since(start: Mark) -> Effort:
    """What this process spent after START. Tools count once reaped; pool work counts once its task returned."""
    end = mark()
    pool = {}
    for name, (seconds, tasks) in end.pool.items():
        before = start.pool.get(name, (0.0, 0))
        if tasks > before[1]:
            pool[name] = (seconds - before[0], tasks - before[1])
    return Effort(end.wall - start.wall, end.main - start.main, end.tools - start.tools, pool)
