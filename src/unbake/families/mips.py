"""MIPS ELF REL pairing and signed immediate arithmetic shared by both families."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from unbake.decomp.symbols import Relocation
from unbake.project.config import Held


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


def _signed(value: int) -> int:
    return (value & 0x7FFF) - (value & 0x8000)


def relocation_value(
    highs: Sequence[Relocation],
    low: Relocation,
    draft_words: Sequence[int],
    target_words: Sequence[int],
    gp: int | None,
) -> tuple[int, int]:
    """Return the relocated address and signed object addend for one REL group."""
    name, offset, kind = low.name, low.offset, low.kind
    for relocation in (*highs, low):
        at = relocation.offset
        if at < 0 or at % 4 or at // 4 >= min(len(target_words), len(draft_words)):
            raise Held("families", f"{name}.offset: no aligned target word at {at}")
        target, draft = target_words[at // 4], draft_words[at // 4]
        if relocation.kind in (5, 6, 7) and (target ^ draft) & 0xFFFF0000:
            raise Held("families", f"{name}: relocation instruction differs at +0x{at:X}")
        if relocation.kind == 5 and (target >> 26 != 15 or draft >> 26 != 15):
            raise Held("families", f"{name}: HI16 requires lui at +0x{at:X}")
    target, draft = target_words[offset // 4], draft_words[offset // 4]
    if kind == 6:
        addresses = {((target_words[high.offset // 4] & 0xFFFF) << 16) + _signed(target) & 0xFFFFFFFF for high in highs}
        addends = {((draft_words[high.offset // 4] & 0xFFFF) << 16) + _signed(draft) & 0xFFFFFFFF for high in highs}
        if len(addresses) != 1 or len(addends) != 1:
            raise Held("families", f"{name}: two-addresses in HI16/LO16 pairs")
        effective, addend = addresses.pop(), addends.pop()
        if addend & 0x80000000:
            addend -= 0x100000000
        return effective, addend
    if kind == 7:
        if gp is None:
            raise Held("families", f"{name}.gp: missing value")
        return (gp + _signed(target)) & 0xFFFFFFFF, _signed(draft)
    if kind == 2:
        return target, draft
    raise Held("families", f"{name}: unsupported relocation {kind}")
