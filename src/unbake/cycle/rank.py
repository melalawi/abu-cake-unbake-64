"""Rank candidates by expected bytes per minute of work, learned from this project's attempts.

score = bytes * p(bucket) / minutes(bucket), where a bucket is floor(log2(bytes)), p is the share of
functions in that bucket that became exact, and minutes is their median time. A bucket with fewer than
min_history functions scores the pooled rate: exact bytes over minutes across all history, as if minutes grow
with size, so it is flat in size and does not favour the largest. Ties and the no-history case go smallest first.
Carryovers (functions with attempts) always come first.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass


@dataclass(frozen=True)
class Candidate:
    function: str
    bytes: int
    versions: tuple[str, ...]
    carryover: bool
    best_percent: float | None


@dataclass(frozen=True)
class History:
    function: str
    bytes: int
    exact: bool
    minutes: float


def bucket(size: int) -> int:
    return int(math.log2(size)) if size > 0 else 0


def _rates(history: list[History]) -> tuple[dict[int, tuple[float, float, int]], float]:
    groups: dict[int, list[History]] = {}
    for row in history:
        groups.setdefault(bucket(row.bytes), []).append(row)
    rates = {
        key: (
            sum(row.exact for row in rows) / len(rows),
            statistics.median(row.minutes for row in rows),
            len(rows),
        )
        for key, rows in groups.items()
    }
    pooled = sum(row.bytes for row in history if row.exact) / max(sum(row.minutes for row in history), 1e-9)
    return rates, pooled


def score(candidate: Candidate, history: list[History], min_history: int) -> float:
    rates, pooled = _rates(history)
    p, minutes, count = rates.get(bucket(candidate.bytes), (0.0, 0.0, 0))
    if count < min_history:
        return pooled
    return candidate.bytes * p / max(minutes, 1e-9)


def rank(
    candidates: list[Candidate], history: list[History], *, min_bytes: int, max_bytes: int, min_history: int
) -> list[Candidate]:
    """Carryovers first (best percent first), then the rest by score, smallest first on ties and without history."""
    window = [row for row in candidates if row.carryover or min_bytes <= row.bytes <= max_bytes]
    carry = sorted(
        (row for row in window if row.carryover), key=lambda row: (-(row.best_percent or 0.0), -row.bytes, row.function)
    )
    fresh = [row for row in window if not row.carryover]
    if history:
        fresh.sort(key=lambda row: (-round(score(row, history, min_history), 9), row.bytes, row.function))
    else:
        fresh.sort(key=lambda row: (row.bytes, row.function))
    return [*carry, *fresh]
