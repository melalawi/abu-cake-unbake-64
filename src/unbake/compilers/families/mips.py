"""MIPS facts shared by both families: ELF REL pairing, the O32 entry state and the code shape contract."""

from __future__ import annotations

import re
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


# Set when an O32 C function starts: zero, arguments a0-a3, gp, sp, ra; floating arguments $f12-$f15.
O32_ENTRY_GPRS = frozenset({0, 4, 5, 6, 7, 28, 29, 31})
O32_ENTRY_FPRS = frozenset({12, 13, 14, 15})
# Every rule work.shape knows; a family leaves one out of Shape.rules when it does not hold for its compiler.
RULES = frozenset({"filler", "frame", "call_ra", "isa", "entry_registers", "tail", "cop0", "fcsr", "kreg"})
# The original-asm rules (work.shape.original), a closed set: instructions no configured compiler emits from C.
ORIGINAL_RULES = frozenset({"cop0", "fcsr", "isa", "kreg"})


@dataclass(frozen=True)
class Shape:
    """What one compiler's emitted functions look like, as the shared shape rules (work.shape) read it."""

    isa_level: int
    entry_gprs: frozenset[int]
    entry_fprs: frozenset[int]
    object_alignment: int
    fragment_bytes: int
    rules: frozenset[str]


def isa_level(compiler: str, cflags: tuple[str, ...]) -> int:
    """The MIPS ISA level the compiler's flags name (the last -mipsN); refused when they name none."""
    levels = [int(flag[5:]) for flag in cflags if re.fullmatch(r"-mips[1-4]", flag)]
    if not levels:
        raise Held("families", f"compilers.{compiler}.cflags: required -mipsN")
    return levels[-1]


def emitters(shapes: Iterable[Shape]) -> Shape:
    """What any of SHAPES (every configured compiler) can emit, for the original-asm rules: the highest ISA level
    and only the rules every one of them switches on. Entry state, alignment and fragment size are not used."""
    shapes = list(shapes)
    if not shapes:
        raise Held("families", "compilers: required at least one configured compiler")
    first = shapes[0]
    rules = frozenset.intersection(*(item.rules for item in shapes)) & ORIGINAL_RULES
    return Shape(max(item.isa_level for item in shapes), first.entry_gprs, first.entry_fprs, 0, 0, rules)


def o32_shape(compiler: str, cflags: tuple[str, ...], *, object_alignment: int, fragment_bytes: int) -> Shape:
    """An O32 compiler's shape with every rule on; a family passes its own alignment and fragment size."""
    return Shape(isa_level(compiler, cflags), O32_ENTRY_GPRS, O32_ENTRY_FPRS, object_alignment, fragment_bytes, RULES)
