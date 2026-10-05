"""Shared shape rules: what a function interval's words say about it, judged against one compiler's Shape.

Every rule here is generic MIPS; what differs per compiler (ISA level, entry registers, object alignment,
fragment size, which rules hold) comes from the compiler family's Shape (compilers.families.*.shape)."""

from __future__ import annotations

import struct
from typing import TYPE_CHECKING

from unbake.config import Held

if TYPE_CHECKING:
    from unbake.compilers.families.mips import Shape
    from unbake.config import Compiler

JR_RA, NOP = 0x03E00008, 0
# MIPS III doubleword opcodes and SPECIAL functions; no -mips1/-mips2 compiler emits them.
_MIPS3_OPS = frozenset({24, 25, 26, 27, 44, 45, 52, 55, 60, 63})
_MIPS3_SPECIAL = frozenset({20, 22, 23, 28, 29, 30, 31, 44, 45, 46, 47, 56, 58, 59, 60, 62, 63})


def for_compiler(compiler: Compiler) -> Shape:
    """The Shape of one configured compiler: its family's adapter built from the compiler's flags."""
    from unbake.compilers.families import family_for

    return family_for(compiler.id).shape(compiler.id, compiler.cflags)


def words_of(data: bytes) -> list[int]:
    if not isinstance(data, bytes) or not data or len(data) % 4:
        raise Held("plan", "words: required nonempty complete big-endian words")
    return [item[0] for item in struct.iter_unpack(">I", data)]


def classify(data: bytes, address: int, shape: Shape) -> tuple[str, str]:
    """Return a route and its word evidence for the interval at VRAM ADDRESS; classification is a work hint."""
    words = words_of(data)
    skip = filler(words, address, shape)
    if skip:
        return "boundary", f"{skip * 4} bytes of alignment filler before the function at +0x{skip * 4:X}"
    returns = [index for index, word in enumerate(words) if word == JR_RA]
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
        reason = not_c(words, shape)
        if reason:
            return "dead", reason
        return "drafter", "one complete return"
    if any(word >> 26 == 0 and word & 63 == 8 for word in words):
        return "drafter", "indirect dispatch"
    return "merge", "no complete return or indirect dispatch"


def _frame_open(word: int) -> bool:
    """addiu sp,sp,-N: an O32 frame allocation."""
    return word >> 16 == 0x27BD and word & 0x8000 != 0


def _calls(word: int) -> bool:
    """JAL or JALR."""
    return word >> 26 == 3 or (word >> 26 == 0 and word & 63 == 9)


def filler(words: list[int], address: int, shape: Shape) -> int:
    """Leading alignment filler, in words: the words before the first object-aligned word when the row starts
    unaligned, that word begins a frame or an empty function (jr ra; nop), and the words before it cannot open a
    C function. 0 when there is none or the shape's `filler` rule is off."""
    if "filler" not in shape.rules:
        return 0
    skip = (-address % shape.object_alignment) // 4
    if not skip or skip >= len(words):
        return 0
    rest = words[skip:]
    if not (_frame_open(rest[0]) or rest[:2] == [JR_RA, NOP]):
        return 0
    return skip if _never_starts_c(words[:skip], shape) else 0


def _never_starts_c(words: list[int], shape: Shape) -> bool:
    """Words that cannot open a C function before its frame or return: a stack adjustment, a call, or a read of
    a register nothing has set at entry."""
    if any(word >> 16 == 0x27BD or _calls(word) for word in words):
        return True
    return _reads_unset(words, shape)


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


def not_c(words: list[int], shape: Shape) -> str | None:
    """Why a short body (at most shape.fragment_bytes) with no balanced frame cannot be compiled C, or None.

    Rules, each switchable per compiler: `frame` (allocated without a release or the reverse), `call_ra` (a call
    without saving ra), `isa` (an opcode above the compiler's ISA level), `entry_registers` (a register read
    before anything sets it). A balanced frame or a longer body is a real function's shape and is never judged."""
    adjusts = [((word & 0xFFFF) ^ 0x8000) - 0x8000 for word in words if word >> 16 == 0x27BD]
    allocated, released = any(value < 0 for value in adjusts), any(value > 0 for value in adjusts)
    if len(words) * 4 > shape.fragment_bytes or (allocated and released):
        return None
    if "frame" in shape.rules and allocated != released:
        return "stack frame allocated or released but not both"
    if "call_ra" in shape.rules and any(_calls(word) for word in words):
        return "call without saving ra"
    if (
        "isa" in shape.rules
        and shape.isa_level < 3
        and any(word >> 26 in _MIPS3_OPS or (word >> 26 == 0 and word & 63 in _MIPS3_SPECIAL) for word in words)
    ):
        return f"64-bit opcode outside -mips{shape.isa_level}"
    if "entry_registers" in shape.rules:
        gprs, fprs = set(shape.entry_gprs), set(shape.entry_fprs)
        for word in words:
            reads, writes, freads, fwrites = _registers(word)
            if reads - gprs:
                return f"reads ${min(reads - gprs)} before setting it"
            if freads - fprs:
                return f"reads $f{min(freads - fprs)} before setting it"
            gprs |= writes
            fprs |= fwrites
    return None


def tail(previous: bytes, previous_address: int, data: bytes, shape: Shape) -> bool:
    """DATA continues the row before it: that row has no complete return and does not end in a jump (every
    compiled function ends with a jump or branch and its delay slot), and DATA opens no frame of its own and reads
    a register before setting it (a value the row before computed), so both are one function the split cut."""
    if "tail" not in shape.rules:
        return False
    words, before = words_of(data), words_of(previous)
    if len(before) < 2 or _transfers(before[-2]) or classify(previous, previous_address, shape)[0] != "merge":
        return False
    return not any(_frame_open(word) for word in words) and _reads_unset(words, shape)


def _reads_unset(words: list[int], shape: Shape) -> bool:
    """A register is read before anything sets it (only the shape's entry registers are set at a C entry)."""
    gprs, fprs = set(shape.entry_gprs), set(shape.entry_fprs)
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
