"""Finite ROM-backed local dispatch edges for entry and storage inference."""

from __future__ import annotations

import struct
from typing import Any

from unbake.config import Held
from unbake.decomp.indexed import indexed_references
from unbake.typemap.mips import control


def targets(words: list[int], address: int, reader: Any) -> dict[int, tuple[int, ...]]:
    """Read only a completely guarded table whose entries stay in this body.

    The selector bound, scaled index, table load and indirect jump must form
    one uninterrupted dispatch. An unresolved or contradictory table remains
    unresolved; a prefix of valid entries is never a complete control proof.
    """
    result = {}
    incoming = {
        target
        for index, word in enumerate(words)
        if (branch := control(word, address + index * 4)) is not None and (target := branch[1]) is not None
    }
    for reference in indexed_references(words):
        index = reference.offset // 4
        if reference.scale != 4 or index < 5 or index + 1 >= len(words):
            continue
        bound, guard, shift, high, add, load, jump = words[index - 5 : index + 2]
        selector = bound >> 21 & 31
        check = bound >> 16 & 31
        scaled = shift >> 11 & 31
        base = high >> 16 & 31
        loaded = load >> 16 & 31
        count = bound & 0xFFFF
        if (
            bound >> 26 != 11
            or not 0 < count <= len(words)
            or guard >> 26 != 4
            or (guard >> 21 & 31) != check
            or (guard >> 16 & 31) != 0
            or shift >> 26 != 0
            or shift & 63 != 0
            or (shift >> 6 & 31) != 2
            or (shift >> 16 & 31) != selector
            or high >> 26 != 15
            or add >> 26 != 0
            or add & 63 != 0x21
            or (add >> 11 & 31) != base
            or {add >> 21 & 31, add >> 16 & 31} != {base, scaled}
            or load >> 26 != 0x23
            or (load >> 21 & 31) != base
            or jump >> 26 != 0
            or jump & 63 != 8
            or (jump >> 21 & 31) != loaded
            or any(address + offset * 4 in incoming for offset in range(index - 4, index + 2))
        ):
            continue
        try:
            span = reader.table_span(reference.address, count * 4)
            binary = reader(reference.address, count * 4)
        except (Held, OSError):
            continue
        if len(binary) != count * 4:
            continue
        entries = tuple((value + span.table_entry_bias) & 0xFFFFFFFF for (value,) in struct.iter_unpack(">I", binary))
        if all(address <= target < address + len(words) * 4 and target % 4 == 0 for target in entries):
            result[address + (index + 1) * 4] = entries
    return result
