"""MIPS ELF REL pairing and signed immediate arithmetic shared by both families."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from unbake.config import Held


@dataclass(frozen=True)
class Relocation:
    offset: int
    kind: int
    name: str


def relocation_pairs(relocations: Iterable[Relocation]) -> list[tuple[Relocation | None, Relocation]]:
    """Match each pending HI16 to the next LO16 for its symbol, in REL order.

    Standalone relocations, including GPREL16, have a None high member.
    """
    pending: dict[str, list[Relocation]] = {}
    pairs: list[tuple[Relocation | None, Relocation]] = []
    for relocation in relocations:
        if not relocation.name:
            raise Held("families", "relocation.name: missing value")
        if relocation.kind == 5:
            pending.setdefault(relocation.name, []).append(relocation)
        elif relocation.kind == 6:
            highs = pending.pop(relocation.name, [])
            if not highs:
                raise Held("families", f"{relocation.name}: LO16 has no HI16 pair")
            pairs.extend((high, relocation) for high in highs)
        else:
            pairs.append((None, relocation))
    if pending:
        raise Held("families", f"{', '.join(sorted(pending))}: HI16 has no LO16 pair")
    return pairs
