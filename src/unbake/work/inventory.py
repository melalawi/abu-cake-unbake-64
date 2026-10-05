"""The function inventory of every version, grouped across versions and classified by body."""

from __future__ import annotations

import re
import struct
from dataclasses import dataclass
from pathlib import Path

from unbake.config import Held, Project
from unbake.layout import split


@dataclass(frozen=True)
class Row:
    function: str
    versions: tuple[str, ...]
    names: dict[str, str]
    aliases: tuple[str, ...]
    size: int
    score: float | None
    identical: bool
    draft: Path | None
    route: str
    evidence: tuple[str, ...]


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise Held("plan", f"{name}: required nonempty value")
    return value


def classify(data: bytes, address: int | None = None, level: int = 0) -> tuple[str, str]:
    """Return a route and its word evidence; classification is a work hint.

    ADDRESS (the row's VRAM start) finds leading alignment filler; LEVEL is the compiler's ISA level (isa())."""
    if not isinstance(data, bytes) or not data or len(data) % 4:
        raise Held("plan", "words: required nonempty complete big-endian words")
    words = [item[0] for item in struct.iter_unpack(">I", data)]
    skip = filler(words, address) if address is not None else 0
    if skip:
        return "boundary", f"{skip * 4} bytes of alignment filler before the function at +0x{skip * 4:X}"
    returns = [index for index, word in enumerate(words) if word == 0x03E00008]
    if len(returns) > 1:
        return "boundary", f"{len(returns)} jr-ra instructions in one interval"
    if not any(words) or words[0] == 0:
        return "boundary", "leading padding"
    if len(words) >= 2 and all(0x80000000 <= word < 0xC0000000 and word % 4 == 0 for word in words):
        return "table", "aligned address table"
    if all(byte == 0 or 32 <= byte < 127 for byte in data) and any(32 <= byte < 127 for byte in data):
        return "table", "text data in a function interval"
    if any(word >> 26 == 16 or word >> 26 == 47 for word in words):
        return "asm", "privileged instruction"
    if returns:
        end = returns[0] + 2
        if end > len(words):
            return "boundary", "jr-ra delay slot outside the interval"
        if len(words) - end >= 2 and not any(words[end:]):
            return "boundary", "padding beyond the return delay slot"
        if any(words[end:]):
            return "boundary", "words beyond the return delay slot"
        reason = not_c(words, level)
        if reason:
            return "dead", reason
        return "drafter", "one complete return"
    if any(word >> 26 == 0 and word & 63 == 8 for word in words):
        return "drafter", "indirect dispatch"
    return "merge", "no complete return or indirect dispatch"


FRAGMENT_BYTES = 64
JR_RA, NOP = 0x03E00008, 0
# Set when a C function starts: zero, arguments a0-a3, gp, sp, ra; floating arguments $f12-$f15.
_SET_GPR = frozenset({0, 4, 5, 6, 7, 28, 29, 31})
_SET_FPR = frozenset({12, 13, 14, 15})
# MIPS III doubleword opcodes and SPECIAL functions; no -mips1/-mips2 compiler emits them.
_MIPS3_OPS = frozenset({24, 25, 26, 27, 44, 45, 52, 55, 60, 63})
_MIPS3_SPECIAL = frozenset({20, 22, 23, 28, 29, 30, 31, 44, 45, 46, 47, 56, 58, 59, 60, 62, 63})


def isa(cflags: tuple[str, ...]) -> int:
    """The MIPS ISA level a compiler's flags name (-mipsN), or 0 when they name none."""
    levels = [int(flag[5:]) for flag in cflags if re.fullmatch(r"-mips[1-4]", flag)]
    return levels[-1] if levels else 0


def _prologue(word: int) -> bool:
    return word >> 16 == 0x27BD and word & 0x8000 != 0


def filler(words: list[int], address: int) -> int:
    """Leading alignment filler: the words before the first 16-byte aligned word when the row starts unaligned,
    that word begins a frame (addiu sp,sp,-N) or an empty function (jr ra; nop), and the words before it cannot
    open a C function. 0 when there is none."""
    skip = (-address % 16) // 4
    if not skip or skip >= len(words):
        return 0
    rest = words[skip:]
    if not (_prologue(rest[0]) or rest[:2] == [JR_RA, NOP]):
        return 0
    return skip if _never_starts_c(words[:skip]) else 0


def _never_starts_c(words: list[int]) -> bool:
    """Words that cannot open a C function before its frame or return: a stack adjustment, a call, or a read of
    a register nothing has set (only arguments, gp, sp and ra are set at entry)."""
    if any(word >> 16 == 0x27BD or word >> 26 == 3 or (word >> 26 == 0 and word & 63 == 9) for word in words):
        return True
    return _reads_unset(words)


def _registers(word: int) -> tuple[set[int], set[int], set[int], set[int]]:
    """(GPRs read, GPRs written, FPRs read, FPRs written) by one instruction; unknown encodings touch none."""
    op, rs, rt, rd = word >> 26, (word >> 21) & 31, (word >> 16) & 31, (word >> 11) & 31
    fs, fd = rd, (word >> 6) & 31
    none: set[int] = set()
    if op == 0:
        function = word & 63
        if function in (8, 9):
            return {rs}, ({rd} if function == 9 else none), none, none
        if function in (0, 2, 3):
            return {rt}, {rd}, none, none
        if function in (16, 18):
            return none, {rd}, none, none
        if function in (17, 19):
            return {rs}, none, none, none
        if function in (24, 25, 26, 27):
            return {rs, rt}, none, none, none
        return {rs, rt}, {rd}, none, none
    if op == 3:
        return none, {2, 3, 31}, none, {0, 2}
    if op == 15:
        return none, {rt}, none, none
    if op in (1, 6, 7, 22, 23):
        return {rs}, none, none, none
    if op in (4, 5, 20, 21):
        return {rs, rt}, none, none, none
    if 8 <= op <= 14 or op in (24, 25, 26, 27, 55) or 32 <= op <= 39:
        return {rs}, {rt}, none, none
    if op in (40, 41, 42, 43, 44, 45, 46, 63):
        return {rs, rt}, none, none, none
    if op in (49, 53):
        return {rs}, none, none, {rt}
    if op in (57, 61):
        return {rs}, none, {rt}, none
    if op == 17:
        if rs == 0:
            return none, {rt}, {fs}, none
        if rs == 4:
            return {rt}, none, none, {fs}
        if rs in (16, 17, 20, 21):
            function = word & 63
            if function >= 48:
                return none, none, {fs, rt}, none
            if function <= 3:
                return none, none, {fs, rt}, {fd}
            return none, none, {fs}, {fd}
    return none, none, none, none


def not_c(words: list[int], level: int) -> str | None:
    """Why a short body (at most FRAGMENT_BYTES) with no balanced frame cannot be compiled C, or None.

    A frame allocated without a release (or the reverse), a call without saving ra, a register read before
    anything sets it (only arguments, gp, sp and ra are set at entry), or an opcode above the compiler's ISA
    level. A balanced frame or a longer body is a real function's shape and is never judged here."""
    adjusts = [((word & 0xFFFF) ^ 0x8000) - 0x8000 for word in words if word >> 16 == 0x27BD]
    allocated, released = any(value < 0 for value in adjusts), any(value > 0 for value in adjusts)
    if len(words) * 4 > FRAGMENT_BYTES or (allocated and released):
        return None
    if allocated != released:
        return "stack frame allocated or released but not both"
    if any(word >> 26 == 3 or (word >> 26 == 0 and word & 63 == 9) for word in words):
        return "call without saving ra"
    if (
        level
        and level < 3
        and any(word >> 26 in _MIPS3_OPS or (word >> 26 == 0 and word & 63 in _MIPS3_SPECIAL) for word in words)
    ):
        return f"64-bit opcode outside -mips{level}"
    gprs, fprs = set(_SET_GPR), set(_SET_FPR)
    for word in words:
        reads, writes, freads, fwrites = _registers(word)
        if reads - gprs:
            return f"reads ${min(reads - gprs)} before setting it"
        if freads - fprs:
            return f"reads $f{min(freads - fprs)} before setting it"
        gprs |= writes
        fprs |= fwrites
    return None


def tail(previous: bytes, previous_address: int, data: bytes) -> bool:
    """DATA continues the row before it: that row has no complete return and does not end in a jump (every
    compiled function ends with a jump or branch and its delay slot), and DATA opens no frame of its own and reads
    a register before setting it (a value the row before computed), so both are one function the split cut."""
    words = [item[0] for item in struct.iter_unpack(">I", data)]
    before = [item[0] for item in struct.iter_unpack(">I", previous)]
    if len(before) < 2 or _transfers(before[-2]) or classify(previous, previous_address)[0] != "merge":
        return False
    return not any(_prologue(word) for word in words) and _reads_unset(words)


def _reads_unset(words: list[int]) -> bool:
    """A register is read before anything sets it (only arguments, gp, sp and ra are set at a C entry)."""
    gprs, fprs = set(_SET_GPR), set(_SET_FPR)
    for word in words:
        reads, writes, freads, fwrites = _registers(word)
        if reads - gprs or freads - fprs:
            return True
        gprs |= writes
        fprs |= fwrites
    return False


def _transfers(word: int) -> bool:
    """A jump or branch: J/JAL, JR/JALR, any conditional branch (BEQ zero,zero is B), or a COP1 branch."""
    op = word >> 26
    return (
        op in (1, 2, 3, 4, 5, 6, 7, 20, 21, 22, 23)
        or (op == 0 and word & 63 in (8, 9))
        or (op == 17 and (word >> 21) & 31 == 8)
    )


def inventory(project: Project) -> tuple[str, list[split.Function], dict[tuple[str, str], bytes]]:
    versions = getattr(project, "versions", None)
    if not isinstance(versions, tuple) or not versions or len(set(versions)) != len(versions):
        raise Held("plan", "project.versions: required distinct VERSIONs")
    reference = _text(getattr(project, "names_from", None), "project.names_from")
    if reference not in versions:
        raise Held("plan", f"project.names_from {reference}: unknown VERSION")
    inventory = [item for version in versions for item in split.functions(project, version)]
    bodies = {(item.version, item.name): split.words(project, item) for item in inventory}
    return reference, inventory, bodies


def groups(inventory: list[split.Function], bodies: dict[tuple[str, str], bytes]) -> list[list[split.Function]]:
    """Shared names join variants; equal bytes join only unique VERSION rows."""
    parent = list(range(len(inventory)))

    def root(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    versions = [{item.version} for item in inventory]

    def join(left: int, right: int) -> None:
        left, right = root(left), root(right)
        if left != right:
            parent[right] = left
            versions[left] |= versions[right]

    aliases: dict[str, int] = {}
    equal: dict[bytes, list[int]] = {}
    for index, item in enumerate(inventory):
        for name in (item.name, *item.aliases):
            if name in aliases:
                join(index, aliases[name])
            else:
                aliases[name] = index
        equal.setdefault(bodies[item.version, item.name], []).append(index)
    for indices in equal.values():
        if len({inventory[index].version for index in indices}) != len(indices):
            continue
        for index in indices[1:]:
            left, right = root(indices[0]), root(index)
            if left != right and versions[left].isdisjoint(versions[right]):
                join(left, right)
    groups: dict[int, list[split.Function]] = {}
    for index, item in enumerate(inventory):
        groups.setdefault(root(index), []).append(item)
    for items in groups.values():
        if len({item.version for item in items}) != len(items):
            raise Held("plan", f"function {items[0].name}: ambiguous VERSION identity")
    return list(groups.values())
