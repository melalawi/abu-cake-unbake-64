"""The pick screen of `unbake cycle` (questionary)."""

from __future__ import annotations

from unbake.config import Held
from unbake.cycle import rank


def _label(row: rank.Candidate) -> str:
    mark = "↻ " if row.carryover else "  "
    best = f"best {row.best_percent:.1f}%" if row.best_percent is not None else "new"
    return f"{mark}{row.function}  {row.bytes} B  {best}  [{' '.join(row.versions)}]"


def pick(order: list[rank.Candidate]) -> list[rank.Candidate]:
    import questionary

    if not order:
        return []
    auto = min(5, len(order))
    choices = [questionary.Choice(f"auto {auto}", value="auto", checked=True)]
    choices += [questionary.Choice(_label(row), value=row.function) for row in order[:200]]
    answer = questionary.checkbox("Pick functions (bytes per minute)", choices=choices, qmark="?").unsafe_ask()
    if not answer:
        raise Held("cycle", "cycle.pick: nothing picked")
    if "auto" in answer:
        return order[:auto]
    chosen = set(answer)
    return [row for row in order if row.function in chosen]
