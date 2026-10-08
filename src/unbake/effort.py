"""Effort of one command or step: wall time, CPU of this process, its tools and its pool work by function, the
peak resident memory of this process and of its workers, and counts a step reports (facts extracted of all).

The pool charges each task's worker CPU and peak RSS to its function (pool.run); a command reports the totals in its
result when it ends, and each step reports its own share. A step opens a memory window (window()), so its peak is
its own; a mark taken before several windows sees the highest of them."""

from __future__ import annotations

import contextlib
import math
import os
import resource
import threading
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from unbake.config import Host

TOP = 5
MB = 1_000_000

_lock = threading.Lock()
# function name -> [worker CPU seconds, tasks]
_ledger: dict[str, list[float]] = {}
# name -> [done, total], summed (facts: source units extracted of all)
_counts: dict[str, list[int]] = {}
_stages: list[tuple[str, float, float]] = []
# per memory window: [main peak, worker peak] in bytes; the last window is open
_windows: list[list[int]] = [[0, 0]]


def host_busy() -> float:
    """CPU-seconds every process on this host has used since boot (/proc/stat), or 0 where it cannot be read."""
    try:
        fields = Path("/proc/stat").read_text().split("\n", 1)[0].split()[1:]
    except OSError:
        return 0.0
    # user nice system idle iowait irq softirq steal: everything but idle and iowait is busy.
    busy = sum(int(value) for index, value in enumerate(fields[:8]) if index not in (3, 4))
    return busy / os.sysconf("SC_CLK_TCK")


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
    # A kernel control write (resets VmHWM to the current RSS), not a file the tool publishes.
    with contextlib.suppress(OSError):
        from unbake import atomic

        atomic.control(Path("/proc/self/clear_refs"), b"5")


def window() -> None:
    """Close the open memory window (keeping its peak) and open a new one."""
    with _lock:
        _windows[-1][0] = max(_windows[-1][0], resident_peak())
        _windows.append([0, 0])
        _reset_peak()


def charge(name: str, seconds: float, rss: int = 0, counts: dict[str, tuple[int, int]] | None = None) -> None:
    """One pool task's CPU, peak resident bytes and the counts it added (the worker's, merged into this ledger)."""
    if (
        type(name) is not str
        or not name
        or type(seconds) not in (int, float)
        or seconds < 0
        or not math.isfinite(seconds)
    ):
        raise ValueError("effort.charge: required named finite nonnegative CPU seconds")
    if type(rss) is not int or rss < 0:
        raise ValueError("effort.charge: required nonnegative integer measured RSS")
    for kind, (done, total) in (counts or {}).items():
        if type(kind) is not str or type(done) is not int or type(total) is not int or not 0 <= done <= total:
            raise ValueError(f"effort.charge.{kind}: invalid counts")
    with _lock:
        row = _ledger.setdefault(name, [0.0, 0])
        row[0] += seconds
        row[1] += 1
        _windows[-1][1] = max(_windows[-1][1], rss)
        for kind, (done, total) in (counts or {}).items():
            added = _counts.setdefault(kind, [0, 0])
            added[0] += done
            added[1] += total


def counted() -> dict[str, tuple[int, int]]:
    """This process's counts so far, by name."""
    with _lock:
        return {name: (row[0], row[1]) for name, row in _counts.items()}


# Items a stage may process in the parent alone before the guard names it; the pool width the command expects.
PARENT_ITEMS = 256
PARENT_ONLY = "parent-only: "
_width = 1


def expect_workers(count: int) -> None:
    """The pool width this command runs with; a stage over PARENT_ITEMS that used no worker is then a finding."""
    global _width
    _width = count


def parent_only(label: str, items: int, pooled: bool) -> bool:
    return _width > 1 and items > PARENT_ITEMS and not pooled


def record_stage(name: str, wall: float, cpu: float) -> None:
    """One finished stage: its wall seconds and the CPU seconds all processes spent in it."""
    with _lock:
        _stages.append((name, wall, cpu))


def count(name: str, done: int, total: int) -> None:
    if type(name) is not str or not name or type(done) is not int or type(total) is not int or not 0 <= done <= total:
        raise ValueError(f"effort.count.{name}: required nonnegative integer done <= total")
    with _lock:
        row = _counts.setdefault(name, [0, 0])
        row[0] += done
        row[1] += total


def name_of(fn: object) -> str:
    module = getattr(fn, "__module__", type(fn).__module__) or ""
    return f"{module.removeprefix('unbake.')}.{getattr(fn, '__qualname__', type(fn).__qualname__)}"


@dataclass(frozen=True)
class Mark:
    wall: float
    main: float
    tools: float
    pool: dict[str, tuple[float, int]]
    window: int = 0
    counts: dict[str, tuple[int, int]] = field(default_factory=dict)
    host: float = 0.0
    stage: int = 0


def mark() -> Mark:
    own = resource.getrusage(resource.RUSAGE_SELF)
    children = resource.getrusage(resource.RUSAGE_CHILDREN)
    with _lock:
        pool = {name: (row[0], int(row[1])) for name, row in _ledger.items()}
        counts = {name: (row[0], row[1]) for name, row in _counts.items()}
        opened = len(_windows) - 1
        staged = len(_stages)
    return Mark(
        time.monotonic(),
        own.ru_utime + own.ru_stime,
        children.ru_utime + children.ru_stime,
        pool,
        opened,
        counts,
        host_busy(),
        staged,
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
    # CPU-seconds other processes on the host used meanwhile (host busy time less this command's).
    external: float = 0.0
    stages: tuple[tuple[str, float, float], ...] = ()

    @property
    def cpu(self) -> float:
        return self.main + self.tools + sum(seconds for seconds, _ in self.pool.values())

    def stage_rows(self) -> list[dict[str, object]]:
        """Each named stage once, in first-seen order: runs, wall seconds and cores used (CPU over wall)."""
        rows: dict[str, list[float]] = {}
        for name, wall, cpu in self.stages:
            row = rows.setdefault(name, [0, 0.0, 0.0])
            row[0] += 1
            row[1] += wall
            row[2] += cpu
        return [
            {
                "name": name,
                "runs": int(r[0]),
                "wall_seconds": round(r[1], 3),
                "cores": round(r[2] / r[1], 2) if r[1] > 0 else 0.0,
            }
            for name, r in rows.items()
        ]

    @property
    def external_cores(self) -> float:
        return self.external / self.wall if self.wall > 0 else 0.0

    @property
    def percent(self) -> float:
        return 100 * self.cpu / self.wall if self.wall > 0 else 0.0

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
            "pool": {
                name: [round(seconds, 3), tasks]
                for name, (seconds, tasks) in sorted(self.pool.items(), key=lambda item: -item[1][0])[:TOP]
            },
            "stages": self.stage_rows(),
            "external_cpu_seconds": round(self.external, 3),
            "external_cores": round(self.external_cores, 2),
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
    spent = Effort(
        end.wall - start.wall, end.main - start.main, end.tools - start.tools, pool, main_rss, worker_rss, counts
    )
    # Pool CPU arrives when a task returns, so a window can count a task that started before it: never negative.
    external = max(0.0, end.host - start.host - spent.cpu) if start.host and end.host else 0.0
    with _lock:
        staged = tuple(_stages[start.stage :])
    return replace(spent, external=external, stages=staged)


def step_findings(name: str, spent: Effort, budgets: Host) -> list[str]:
    """A step over the host's [budgets], each named by its budget key: its memory, its CPU, and a single-core
    stretch (this process's CPU above main_cpu_fraction of the step's, once it passes main_cpu_seconds)."""
    found = [
        f"budget.parent_only: {row[0].removeprefix(PARENT_ONLY)} ran in the parent process with workers > 1"
        for row in spent.stages
        if row[0].startswith(PARENT_ONLY)
    ]
    for field_name, value, limit in (
        ("main_rss_bytes", spent.main_rss, budgets.main_rss_bytes),
        ("worker_rss_bytes", spent.worker_rss, budgets.worker_rss_bytes),
    ):
        if value > limit:
            found.append(f"budget.{field_name}: {name} peaked at {value / MB:.0f} MB, over {limit / MB:.0f} MB")
    if spent.cpu > budgets.step_cpu_seconds:
        found.append(f"budget.step_cpu_seconds: {name} spent {spent.cpu:.1f} cpu-s, over {budgets.step_cpu_seconds:g}")
    if spent.main > budgets.main_cpu_seconds and spent.main > budgets.main_cpu_fraction * spent.cpu:
        found.append(
            f"budget.main_cpu_fraction: {name} spent {spent.main:.1f} of {spent.cpu:.1f} cpu-s in one process "
            f"({spent.main / spent.cpu:.0%}), over {budgets.main_cpu_fraction:.0%}"
        )
    return found


def contended(spent: Effort, budgets: Host) -> bool:
    """Other processes kept at least contended_cores busy on average while this ran."""
    return spent.wall > 0 and spent.external / spent.wall >= budgets.contended_cores


def chain_findings(kind: str, spent: Effort, budgets: Host) -> tuple[list[str], list[str]]:
    """A step chain against the host's [budgets]: (findings, contended). KIND is recompute, unchanged or changed.

    The verdict is CPU time, which other processes cannot inflate. A wall-time miss is a finding too, unless other
    processes kept contended_cores busy meanwhile: then it is reported as contended, not a failure. An unforced
    solve's facts misses count against facts_miss_fraction."""
    found: list[str] = []
    notes: list[str] = []
    seconds, cpu = {
        "recompute": (budgets.recompute_seconds, budgets.recompute_cpu_seconds),
        "unchanged": (budgets.unchanged_seconds, budgets.unchanged_cpu_seconds),
        "changed": (budgets.changed_seconds, budgets.changed_cpu_seconds),
    }[kind]
    if spent.cpu > cpu:
        found.append(f"budget.{kind}_cpu_seconds: the {kind} chain spent {spent.cpu:.1f} cpu-s, over {cpu:g}")
    if spent.wall > seconds:
        line = f"budget.{kind}_seconds: the {kind} chain took {spent.wall:.1f} s, over {seconds:g} s"
        if contended(spent, budgets):
            notes.append(f"contended {line} (other processes used {spent.external_cores:.1f} cores)")
        else:
            found.append(line)
    done, total = spent.counts.get("facts", (0, 0))
    fraction = budgets.facts_miss_fraction
    if kind != "recompute" and total and done / total > fraction:
        found.append(
            f"budget.facts_miss_fraction: {done} of {total} source units extracted ({done / total:.2%}), "
            f"over {fraction:.2%}"
        )
    return found, notes
