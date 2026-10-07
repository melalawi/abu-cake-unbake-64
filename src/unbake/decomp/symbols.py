"""Derive data bindings from MIPS relocations and return native Splat edits."""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TypeVar

from unbake.config import Held
from unbake.decomp.needs import SymbolNeed
from unbake.process import named as cause_named


@dataclass(frozen=True)
class Binding:
    name: str
    address: int
    section: str
    type: str
    size: int


@dataclass(frozen=True)
class DataRow:
    name: str
    start: int
    end: int
    section: str


@dataclass(frozen=True)
class Reference:
    address: int
    type: str
    size: int
    offset: int


_LOADS = {
    0x20: ("s8", 1),
    0x21: ("s16", 2),
    0x23: ("s32", 4),
    0x24: ("u8", 1),
    0x25: ("u16", 2),
    0x28: ("s8", 1),
    0x29: ("s16", 2),
    0x2B: ("s32", 4),
    0x31: ("f32", 4),
    0x35: ("f64", 8),
    0x37: ("s64", 8),
    0x39: ("f32", 4),
    0x3D: ("f64", 8),
    0x3F: ("s64", 8),
}
_NAME = r"[A-Za-z_.$][\w.$]*"


T = TypeVar("T")


def required(value: T | None, name: str) -> T:
    if value is None or value == "":
        raise Held(cause_named(f"{name}", f"{name}: missing value", owner="decomp.symbols", stage="symbols"))
    return value


def _signed(value: int) -> int:
    return (value & 0x7FFF) - (value & 0x8000)


def references(target_words: Sequence[int], gp: int | None) -> list[Reference]:
    """Find absolute memory accesses whose base is proved by constant instructions."""
    required(target_words, "target_words")
    constants = {0: 0}
    if gp is not None:
        constants[28] = gp
    result: list[Reference] = []
    reset_after_slot = False
    for index, word in enumerate(target_words):
        reset_now, reset_after_slot = reset_after_slot, False
        if type(word) is not int or not 0 <= word <= 0xFFFFFFFF:
            raise Held(
                cause_named(
                    f"target_words[{index}]",
                    f"target_words[{index}]: expected unsigned word",
                    owner="decomp.symbols",
                    stage="symbols",
                )
            )
        op, rs, rt = word >> 26, word >> 21 & 31, word >> 16 & 31
        immediate = word & 0xFFFF
        if op in _LOADS and rs in constants:
            address = (constants[rs] + _signed(immediate)) & 0xFFFFFFFF
            if address >= 0x80000000:
                type_, size = _LOADS[op]
                result.append(Reference(address, type_, size, index * 4))
        if op == 15 and rt:
            constants[rt] = immediate << 16
        elif op in (9, 13) and rt:
            if rs in constants:
                constants[rt] = (
                    (constants[rs] + _signed(immediate)) if op == 9 else constants[rs] | immediate
                ) & 0xFFFFFFFF
            else:
                constants.pop(rt, None)
        elif op == 0:
            rd, function = word >> 11 & 31, word & 63
            if function in (0x21, 0x25) and rd and rs in constants and rt in constants:
                constants[rd] = (
                    (constants[rs] + constants[rt]) if function == 0x21 else constants[rs] | constants[rt]
                ) & 0xFFFFFFFF
            elif rd:
                constants.pop(rd, None)
        elif op in (2, 3) or op in (1, 4, 5, 6, 7, 0x14, 0x15, 0x16, 0x17):
            reset_after_slot = True
        elif op not in (0x28, 0x29, 0x2B, 0x39, 0x3D, 0x3F, 0x31, 0x35, 0x11) and rt:
            constants.pop(rt, None)
        if op == 0 and word & 63 in (8, 9):
            reset_after_slot = True
        if reset_now:
            constants = {0: 0, **({28: gp} if gp is not None else {})}
    return result


def symbol_line(need: SymbolNeed) -> str:
    """Render the exact Splat symbol declaration used in edits and guidance."""
    for field in ("version", "name", "address", "section", "type", "size"):
        required(getattr(need, field), f"{need.name}.{field}")
    if not re.fullmatch(_NAME, need.name):
        raise Held(
            cause_named(
                f"{need.name}.name", f"{need.name}.name: invalid symbol", owner="decomp.symbols", stage="symbols"
            )
        )
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", need.type):
        raise Held(
            cause_named(
                f"{need.name}.type", f"{need.name}.type: invalid Splat type", owner="decomp.symbols", stage="symbols"
            )
        )
    address_only = (need.type, need.size) == ("address", 0)
    if (
        not 0 <= need.address <= 0xFFFFFFFF
        or (need.size <= 0 and not address_only)
        or (need.type == "address" and not address_only)
    ):
        raise Held(
            cause_named(
                f"{need.name}.address/size",
                f"{need.name}.address/size: invalid range",
                owner="decomp.symbols",
                stage="symbols",
            )
        )
    if address_only:
        return f"{need.name} = 0x{need.address:08X};"
    return f"{need.name} = 0x{need.address:08X}; // type:{need.type} size:0x{need.size:X}"
