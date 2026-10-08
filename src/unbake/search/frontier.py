"""Bounded deterministic retention, separate from the display winner."""

from __future__ import annotations

import math
from typing import Any


def coordinates(trial: Any) -> tuple[float, ...]:
    values = list(trial.compares.values())
    if not values:
        return (math.inf,) * 6
    return (
        sum(m.strict["positional_words"] if m.strict.get("available") else math.inf for m in values),
        sum(
            (m.typed or {}).get("inserted", 0) + (m.typed or {}).get("missing", 0) if m.available else math.inf
            for m in values
        ),
        sum((m.typed or {}).get("changed", 0) if m.available else math.inf for m in values),
        sum((m.typed or {}).get("register", 0) if m.available else math.inf for m in values),
        sum((m.typed or {}).get("relocation", 0) if m.available else math.inf for m in values),
        len(trial.preconditions),
    )


def dominates(left: Any, right: Any) -> bool:
    a, b = coordinates(left), coordinates(right)
    return all(math.isfinite(x) and x <= y for x, y in zip(a, b, strict=True)) and any(
        math.isfinite(y) and x < y for x, y in zip(a, b, strict=True)
    )


def retain(pool: list[Any], width: int, baseline: Any) -> list[Any]:
    if width < 4:
        raise ValueError("search.frontier: minimum width is four")
    ordered = sorted(pool, key=lambda row: (coordinates(row.trial), row.identity))
    frontier = [
        row for row in ordered if not any(dominates(other.trial, row.trial) for other in ordered if other is not row)
    ]
    # Each strategy's representative remains a parent even when another scalar
    # coordinate worsens; the baseline preserves the original composition arm.
    representatives = {baseline.kind: baseline}
    for row in ordered:
        if row.kind in {"conversion-scope", "tail-duplicate", "composition"}:
            representatives.setdefault(row.kind, row)
    result = list(representatives.values())
    for row in frontier:
        if row not in result:
            result.append(row)
    return result[:width]
