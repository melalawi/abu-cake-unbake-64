"""Effort of one command or step: wall time, CPU of this process, its tools and its pool work by function, the
peak resident memory of this process and of its workers, and counts a step reports (facts extracted of all).

The pool charges each task's worker CPU and peak RSS to its function (pool.run); a command reports the totals on
stderr when it ends, and each step reports its own share. A step opens a memory window (window()), so its peak is
its own; a mark taken before several windows sees the highest of them."""

from __future__ import annotations

import contextlib
import resource
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from unbake.config import Budgets

TOP = 5
MB = 1_000_000

_lock = threading.Lock()
# function name -> [worker CPU seconds, tasks]
_ledger: dict[str, list[float]] = {}
# name -> [done, total], summed (facts: source units extracted of all)
_counts: dict[str, list[int]] = {}
# per memory window: [main peak, worker peak] in bytes; the last window is open
_windows: list[list[int]] = [[0, 0]]


def resident_peak() -> int:
    """This process's peak resident bytes since the last reset (VmHWM), else its lifetime peak."""
    try:
        for line in Path("/proc/self/status").read_text().splitlines():
            if line.startswith("VmHWM:"):
                return int(line.split()[1]) * 1024
    except OSError:
        pass
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024


def _reset_peak() -> None:
    with contextlib.suppress(OSError):
        Path("/proc/self/clear_refs").write_text("5")  # resets VmHWM to the current RSS


def window() -> None:
    """Close the open memory window (keeping its peak) and open a new one."""
    with _lock:
        _windows[-1][0] = max(_windows[-1][0], resident_peak())
        _windows.append([0, 0])
        _reset_peak()


def charge(name: str, seconds: float, rss: int = 0) -> None:
    with _lock:
        row = _ledger.setdefault(name, [0.0, 0])
        row[0] += seconds
        row[1] += 1
        _windows[-1][1] = max(_windows[-1][1], rss)


def count(name: str, done: int, total: int) -> None:
    with _lock:
        row = _counts.setdefault(name, [0, 0])
        row[0] += done
        row[1] += total


def name_of(fn: object) -> str:
    module = getattr(fn, "__module__", "") or ""
    return f"{module.removeprefix('unbake.')}.{getattr(fn, '__qualname__', repr(fn))}"


@dataclass(frozen=True)
class Mark:
    wall: float
    main: float
    tools: float
    pool: dict[str, tuple[float, int]]
    window: int = 0
    counts: dict[str, tuple[int, int]] = field(default_factory=dict)


def mark() -> Mark:
    own = resource.getrusage(resource.RUSAGE_SELF)
    children = resource.getrusage(resource.RUSAGE_CHILDREN)
    with _lock:
        pool = {name: (row[0], int(row[1])) for name, row in _ledger.items()}
        counts = {name: (row[0], row[1]) for name, row in _counts.items()}
        opened = len(_windows) - 1
    return Mark(
        time.monotonic(), own.ru_utime + own.ru_stime, children.ru_utime + children.ru_stime, pool, opened, counts
    )


@dataclass(frozen=True)
class Effort:
    wall: float
    main: float
    tools: float
    pool: dict[str, tuple[float, int]]
    main_rss: int = 0
    worker_rss: int = 0
    counts: dict[str, tuple[int, int]] = field(default_factory=dict)

    @property
    def cpu(self) -> float:
        return self.main + self.tools + sum(seconds for seconds, _ in self.pool.values())

    @property
    def percent(self) -> float:
        return 100 * self.cpu / self.wall if self.wall > 0 else 0.0

    def line(self) -> str:
        pooled = sum(seconds for seconds, _ in self.pool.values())
        text = (
            f"effort: {self.wall:.1f} s wall, {self.cpu:.1f} cpu-s ({self.percent:.0f}%): main {self.main:.1f}, "
            f"tools {self.tools:.1f}, pool {pooled:.1f}; peak RSS main {self.main_rss / MB:.0f} MB, "
            f"worker {self.worker_rss / MB:.0f} MB"
        )
        top = sorted(self.pool.items(), key=lambda item: -item[1][0])[:TOP]
        if top:
            text += " [" + ", ".join(f"{name} {seconds:.1f} x{tasks}" for name, (seconds, tasks) in top) + "]"
        return text

    def document(self) -> dict[str, object]:
        return {
            "wall_seconds": round(self.wall, 3),
            "cpu_seconds": round(self.cpu, 3),
            "cpu_percent": round(self.percent, 1),
            "main_cpu_seconds": round(self.main, 3),
            "tools_cpu_seconds": round(self.tools, 3),
            "pool_cpu_seconds": round(sum(seconds for seconds, _ in self.pool.values()), 3),
            "main_rss_bytes": self.main_rss,
            "worker_rss_bytes": self.worker_rss,
            "counts": {name: list(value) for name, value in sorted(self.counts.items())},
        }


def since(start: Mark) -> Effort:
    """What this process spent after START. Tools count once reaped; pool work counts once its task returned."""
    end = mark()
    pool = {}
    for name, (seconds, tasks) in end.pool.items():
        before = start.pool.get(name, (0.0, 0))
        if tasks > before[1]:
            pool[name] = (seconds - before[0], tasks - before[1])
    counts = {}
    for name, (done, total) in end.counts.items():
        before_count = start.counts.get(name, (0, 0))
        if total > before_count[1]:
            counts[name] = (done - before_count[0], total - before_count[1])
    with _lock:
        _windows[-1][0] = max(_windows[-1][0], resident_peak())
        seen = _windows[start.window :]
        main_rss, worker_rss = max(row[0] for row in seen), max(row[1] for row in seen)
    return Effort(
        end.wall - start.wall, end.main - start.main, end.tools - start.tools, pool, main_rss, worker_rss, counts
    )


def step_findings(name: str, spent: Effort, budgets: Budgets) -> list[str]:
    """A step's memory over the project's budgets, each named by its budget key."""
    found = []
    for field_name, value, limit in (
        ("main_rss_bytes", spent.main_rss, budgets.main_rss_bytes),
        ("worker_rss_bytes", spent.worker_rss, budgets.worker_rss_bytes),
    ):
        if value > limit:
            found.append(f"budget.{field_name}: {name} peaked at {value / MB:.0f} MB, over {limit / MB:.0f} MB")
    return found


def chain_findings(kind: str, spent: Effort, budgets: Budgets) -> list[str]:
    """A step chain over the project's budgets. KIND is recompute, unchanged or changed; an unforced solve's facts
    misses count against facts_miss_fraction."""
    found = []
    limit = {
        "recompute": budgets.recompute_seconds,
        "unchanged": budgets.unchanged_seconds,
        "changed": budgets.changed_seconds,
    }[kind]
    if spent.wall > limit:
        found.append(f"budget.{kind}_seconds: the {kind} chain took {spent.wall:.1f} s, over {limit:g} s")
    done, total = spent.counts.get("facts", (0, 0))
    fraction = budgets.facts_miss_fraction
    if kind != "recompute" and total and done / total > fraction:
        found.append(
            f"budget.facts_miss_fraction: {done} of {total} source units extracted ({done / total:.2%}), "
            f"over {fraction:.2%}"
        )
    return found
