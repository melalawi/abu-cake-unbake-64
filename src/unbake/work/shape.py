"""Shared shape rules: what a function interval's words say about it, judged against one compiler's Shape.

Every rule here is generic MIPS; what differs per compiler (ISA level, entry registers, object alignment,
fragment size, which rules hold) comes from the compiler family's Shape (compilers.families.*.shape)."""

from __future__ import annotations

import struct
from collections.abc import Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from unbake.config import Held

if TYPE_CHECKING:
    from unbake.compilers.families.mips import Shape
    from unbake.config import Compiler, Project
    from unbake.layout.boundary import Boundary

JR_RA, NOP = 0x03E00008, 0
# MIPS III doubleword opcodes and SPECIAL functions; no -mips1/-mips2 compiler emits them.
_MIPS3_OPS = frozenset({24, 25, 26, 27, 44, 45, 52, 55, 60, 63})
_MIPS3_SPECIAL = frozenset({20, 22, 23, 28, 29, 30, 31, 44, 45, 46, 47, 56, 58, 59, 60, 62, 63})
# COP0 moves (mfc0, dmfc0, mtc0, dmtc0) by rs, and CO functions (tlbr, tlbwi, tlbwr, tlbp, eret) by funct.
_COP0_MOVES = {0: "mfc0", 1: "dmfc0", 4: "mtc0", 5: "dmtc0"}
_COP0_CO = {1: "tlbr", 2: "tlbwi", 6: "tlbwr", 8: "tlbp", 24: "eret"}
# COP1 fmt S, D, W, L functions that convert: round/trunc/ceil/floor (8-15) and cvt.* (32-37).
_CONVERSIONS = frozenset({*range(8, 16), *range(32, 38)})


@dataclass(frozen=True)
class Original:
    """Why a body is original asm: the rule that proved it (one of mips.ORIGINAL_RULES) and its word evidence."""

    rule: str
    evidence: str


def for_compiler(compiler: Compiler, flags: tuple[str, ...]) -> Shape:
    """The Shape of one configured compiler: its family's adapter built from the compiler's flags."""
    from unbake.compilers.families import family_for

    return family_for(compiler.id).shape(compiler.id, flags)


def emitters(shapes: Iterable[Shape]) -> Shape:
    """What any configured compiler can emit (compilers.families.mips.emitters), for the original-asm rules."""
    from unbake.compilers.families.mips import emitters as combined

    return combined(shapes)


def configured(project: Project) -> tuple[dict[str, Shape], Shape]:
    targets = {
        unit: for_compiler(project.compiler_for(unit), (*project.compiler_for(unit).cflags, *flags))
        for unit, flags in project.unit_flags.items()
    }
    base = {ident: for_compiler(compiler, compiler.cflags) for ident, compiler in project.compilers.items()}
    return {**base, **targets}, emitters([*base.values(), *targets.values()])


def words_of(data: bytes) -> list[int]:
    if not isinstance(data, bytes) or not data or len(data) % 4:
        raise Held("plan", "words: required nonempty complete big-endian words")
    return [item[0] for item in struct.iter_unpack(">I", data)]


def classify(data: bytes, address: int, shape: Shape, emitted: Shape, owned: Boundary) -> tuple[str, str]:
    """Return a route and its word evidence for the interval at VRAM ADDRESS; classification is a work hint.

    SHAPE is the function's own compiler; EMITTED is what any configured compiler emits (emitters), which alone
    decides the `original` route: words no configured compiler can produce from C."""
    words = words_of(data)
    if not owned.proven:
        return "boundary", "; ".join(owned.unproven)
    found = original(words, emitted)
    if found is not None:
        return "original", f"{found.rule}: {found.evidence}"
    reason = _fragment(words, shape)
    if reason is not None:
        return "dead", reason
    return "drafter", "proved single-entry body"


def _cop0(word: int) -> str | None:
    """The COP0 or cache mnemonic WORD encodes; None for any other word (op 16 with another rs is not code)."""
    op, rs = word >> 26, (word >> 21) & 31
    if op == 47:
        return "cache"
    if op != 16:
        return None
    if rs in _COP0_MOVES:
        return _COP0_MOVES[rs]
    if rs == 16 and not (word >> 6) & 0x7FFFF:
        return _COP0_CO.get(word & 63)
    return None


def _fcsr(word: int) -> str | None:
    """cfc1 or ctc1 on $31 (the FCSR)."""
    if word >> 26 != 17 or (word >> 11) & 31 != 31 or word & 0x7FF:
        return None
    return {2: "cfc1", 6: "ctc1"}.get((word >> 21) & 31)


def _converts(word: int) -> bool:
    return word >> 26 == 17 and (word >> 21) & 31 in (16, 17, 20, 21) and word & 63 in _CONVERSIONS


def _kreg(words: list[int]) -> int | None:
    """The index of the first read of k0 or k1 ($26, $27) that an earlier instruction in the body wrote, or None.

    Compilers never allocate the kernel registers. A def-use pair is required, so one stray data word whose
    fields happen to name $26 (a row cut a word early) is not enough."""
    written: set[int] = set()
    for index, word in enumerate(words):
        reads, writes, _, _ = _registers(word)
        if reads & written:
            return index
        written |= writes & {26, 27}
    return None


def _returns(word: int) -> bool:
    """jr (any register) or eret: every function and exception path leaves through one."""
    return (word >> 26 == 0 and word & 0xFC1FFFFF == 8) or word == 0x42000018


def original(words: list[int], emitted: Shape) -> Original | None:
    """The original-asm rule WORDS prove against EMITTED (what any configured compiler emits), or None.

    Only a body that leaves through jr or eret is judged, so mis-split data never qualifies. Rules, in order,
    each only where every configured compiler's family switches it on:
    `cop0`: a COP0 move, TLB op, eret or cache (other op-16 words are not code).
    `fcsr`: cfc1/ctc1 on $31 with no float conversion in the body (compilers touch FCSR only around one).
    `kreg`: k0 or k1 written, then read (a def-use pair, never one stray word).
    `isa`: an opcode above the highest configured -mipsN."""
    if not any(_returns(word) for word in words):
        return None
    rules = emitted.rules
    for index, word in enumerate(words):
        name = _cop0(word) if "cop0" in rules else None
        if name:
            return Original("cop0", f"{name} at +0x{index * 4:X}")
    if "fcsr" in rules and not any(_converts(word) for word in words):
        for index, word in enumerate(words):
            name = _fcsr(word)
            if name:
                return Original("fcsr", f"{name} $31 at +0x{index * 4:X} with no float conversion")
    used = _kreg(words) if "kreg" in rules else None
    if used is not None:
        return Original("kreg", f"k0/k1 set and read at +0x{used * 4:X}")
    if "isa" in rules and emitted.isa_level < 3:
        for index, word in enumerate(words):
            if word >> 26 in _MIPS3_OPS or (word >> 26 == 0 and word & 63 in _MIPS3_SPECIAL):
                return Original("isa", f"64-bit opcode at +0x{index * 4:X} above -mips{emitted.isa_level}")
    return None


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
        if function in (12, 13):
            return none, none, none, none
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
    if 8 <= op <= 14 or op in (24, 25, 26, 27, 48, 52, 55) or 32 <= op <= 39:
        return {rs}, {rt}, none, none
    if op in (56, 60):
        return {rs, rt}, {rt}, none, none
    if op in (40, 41, 42, 43, 44, 45, 46, 63):
        return {rs, rt}, none, none, none
    if op in (49, 53):
        return {rs}, none, none, {rt}
    if op in (57, 61):
        return {rs}, none, {rt}, none
    if op == 16:
        if rs in (0, 1, 2):
            return none, {rt}, none, none
        if rs in (4, 5, 6):
            return {rt}, none, none, none
    if op == 17:
        if rs in (0, 1):
            return none, {rt}, {fs}, none
        if rs in (4, 5):
            return {rt}, none, none, {fs}
        if rs == 2:
            return none, {rt}, none, none
        if rs == 6:
            return {rt}, none, none, none
        if rs in (16, 17, 20, 21):
            function = word & 63
            if function >= 48:
                return none, none, {fs, rt}, none
            if function <= 3:
                return none, none, {fs, rt}, {fd}
            return none, none, {fs}, {fd}
    return none, none, none, none


def _fragment(words: list[int], shape: Shape) -> str | None:
    """Retain compiler capability checks for short, frameless fragments.

    Frame/return ownership belongs exclusively to boundary.closure. These are
    the existing adapter-controlled register/ISA checks, not another boundary
    heuristic. Branches and tail calls are never judged by straight-line liveness.
    """
    if len(words) * 4 > shape.fragment_bytes or any(_frame_open(word) for word in words):
        return None
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
    if "zero_write" in shape.rules and any(word and 0 in _registers(word)[1] for word in words):
        return "writes $zero"
    if "dead_write" not in shape.rules or len(words) < 2 or words[-2] != JR_RA:
        return None
    if any(_transfers(word) for word in words[:-2]):
        return None
    live_gprs, live_fprs = {2, 3, 29, 31}, {0, 2}
    for word in reversed(words):
        reads, writes, freads, fwrites = _registers(word)
        restores = {*range(16, 24), 30} if word >> 26 in range(32, 40) else set()
        unused = writes - {0, 2, 3, 29, 31} - restores - live_gprs
        funused = fwrites - {0, 2} - live_fprs
        if unused:
            return f"writes ${min(unused)} and never reads it"
        if funused:
            return f"writes $f{min(funused)} and never reads it"
        live_gprs = (live_gprs - writes) | reads
        live_fprs = (live_fprs - fwrites) | freads
    return None


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
