"""Boundary evidence shared by loaded layout creation and extracted-text audit."""

from __future__ import annotations

import struct
from dataclasses import dataclass

from unbake.compilers.families.mips import Shape
from unbake.layout import boundary_signatures
from unbake.layout.boundary_signatures import Signature
from unbake.work.shape import _never_starts_c


@dataclass(frozen=True)
class Boundary:
    start: int
    end: int
    tags: tuple[str, ...]
    unproven: tuple[str, ...]

    @property
    def proven(self) -> bool:
        return not self.unproven


def entries(data: bytes, begin: int, end: int, bias: int, signatures: tuple[Signature, ...]) -> dict[int, set[str]]:
    """Seed library signatures before interpreting calls or compiler shapes."""
    result = {
        start: {f"library-signature:{signature.name}:{signature.source}"}
        for start, signature in boundary_signatures.matches(data, begin, end, signatures).items()
    }
    result.setdefault(begin, set()).add("loaded-entry")
    for offset in range(begin, end - 3, 4):
        word = struct.unpack_from(">I", data, offset)[0]
        if word >> 26 == 3:
            target = (((offset + bias + 4) & 0xF0000000) | ((word & 0x3FFFFFF) << 2)) - bias
            if begin <= target < end:
                result.setdefault(target, set()).add("jal-target")
    return result


def shape(words: dict[int, int], start: int, end: int) -> tuple[str, ...]:
    tags = []
    first = words.get(start, 0)
    if first & 0xFFFF0000 in (0x27BD0000, 0x67BD0000) and first & 0x8000:
        tags.append("compiler-stack-prologue")
    if any(word == 0x03E00008 for offset, word in words.items() if start <= offset < end):
        tags.append("return-epilogue")
    return tuple(tags)


@dataclass(frozen=True)
class _Frame:
    sp: int = 0
    ra: bool = True
    saved: tuple[tuple[int, tuple[int, bool]], ...] = ()
    fp: int | None = None
    guarded: bool = False


def _likely(word: int) -> bool:
    op, rs, rt = word >> 26, word >> 21 & 31, word >> 16 & 31
    return op in (20, 21, 22, 23) or (op == 1 and rt in (2, 3, 18, 19)) or (op == 17 and rs == 8 and rt & 2 != 0)


def closure(
    words: dict[int, int],
    start: int,
    end: int,
    bias: int,
    known: set[int],
    jump_tables: dict[int, tuple[int, ...]] | None = None,
) -> tuple[set[int], tuple[str, ...], tuple[str, ...]]:
    """Prove each reachable path with architectural delay/annul and exact frame ownership."""
    from dataclasses import replace

    from unbake.layout.split_analysis import instruction
    from unbake.work.shape import _registers, _transfers

    pending = [(start, _Frame())]
    visited: set[tuple[int, _Frame]] = set()
    balances: dict[int, tuple[int, int | None]] = {}
    seen: set[int] = set()
    tags: set[str] = set()
    failures: set[str] = set()

    def at(offset: int) -> str:
        return f"0x{offset + bias:X}"

    def execute(offset: int, frame: _Frame) -> _Frame | None:
        word = words.get(offset)
        if word is None or not instruction(word):
            failures.add(f"data-or-missing-instruction:{at(offset)}")
            return None
        seen.add(offset)
        op, rs, rt = word >> 26, word >> 21 & 31, word >> 16 & 31
        immediate = word & 65535
        immediate -= 65536 if immediate & 32768 else 0
        if op == 0 and word & 63 in (12, 13):
            if frame.guarded and word & 63 == 13:
                tags.add(f"guarded-trap-terminal:{at(offset)}")
            else:
                failures.add(f"unproved-trap-terminal:{at(offset)}")
            return None
        saved = dict(frame.saved)
        if op in (40, 41, 42, 43, 44, 45, 46, 56, 57, 60, 61, 63) and rs in (29, 30):
            base = frame.sp if rs == 29 else frame.fp
            if base is None:
                failures.add(f"unresolved-frame-store:{at(offset)}")
                return None
            address = base + immediate
            width = {40: 1, 41: 2, 44: 8, 45: 8, 60: 8, 61: 8, 63: 8}.get(op, 4)
            saved = {
                slot: value
                for slot, value in saved.items()
                if not (address < slot + value[0] and slot < address + width)
            }
            if rt == 31 and op in (43, 63):
                if not frame.sp <= address < address + width <= 0:
                    failures.add(f"return-address-save-outside-frame:{at(offset)}")
                    return None
                saved[address] = width, frame.ra
            frame = replace(frame, saved=tuple(sorted(saved.items())))
        if op in (35, 55) and rt == 31 and rs in (29, 30):
            base = frame.sp if rs == 29 else frame.fp
            return replace(frame, ra=base is not None and saved.get(base + immediate) == (8 if op == 55 else 4, True))
        _, writes, _, _ = _registers(word)
        if rt == 29 and rs == 29 and op in (9, 25):
            frame = replace(frame, sp=frame.sp + immediate)
            if frame.sp > 0:
                failures.add(f"frame-restored-above-entry:{at(offset)}")
                return None
        elif 29 in writes:
            # move sp,fp is the only non-immediate restoration whose value is owned here.
            if op == 0 and word & 63 in (33, 37, 45) and {rs, rt} == {0, 30} and frame.fp is not None:
                frame = replace(frame, sp=frame.fp)
            else:
                failures.add(f"unresolved-stack-write:{at(offset)}")
                return None
        if 30 in writes:
            if op == 0 and word & 63 in (33, 37, 45) and {rs, rt} == {0, 29}:
                frame = replace(frame, fp=frame.sp)
            else:
                frame = replace(frame, fp=None)
        if 31 in writes:
            frame = replace(frame, ra=False)
        return frame

    def terminal(offset: int, before: _Frame, after: _Frame, target: int | None = None) -> None:
        if after.sp != 0:
            failures.add(f"compiler-frame-imbalance:{at(offset)}:{after.sp}")
        if not before.ra:
            failures.add(f"returned-ra-not-owned:{at(offset)}")
        if after.sp == 0 and before.ra:
            tags.add("control-flow-closed-return" if target is None else f"tail-call:{at(target)}")

    while pending:
        offset, frame = pending.pop()
        if (offset, frame) in visited:
            continue
        if not start <= offset < end or offset % 4:
            failures.add(f"merge-or-fallthrough:{at(offset)}")
            continue
        balance = frame.sp, frame.fp
        if offset in balances and balances[offset] != balance:
            failures.add(f"inconsistent-frame-state:{at(offset)}")
            continue
        balances[offset] = balance
        visited.add((offset, frame))
        word = words.get(offset)
        if word is None or not instruction(word):
            failures.add(f"data-or-missing-instruction:{at(offset)}")
            continue
        seen.add(offset)
        op, rt, rs = word >> 26, word >> 16 & 31, word >> 21 & 31
        indirect = op == 0 and word & 63 in (8, 9)
        link_register = word >> 11 & 31 if indirect and word & 63 == 9 else None
        if link_register not in (None, 0, 31):
            failures.add(f"unresolved-call-link-register:${link_register}:{at(offset)}")
            continue
        nonlinking = indirect and (word & 63 == 8 or link_register == 0)
        branch = op in (1, 4, 5, 6, 7, 20, 21, 22, 23) or (op == 17 and rs == 8)
        if op in (2, 3) or branch or indirect:
            delay = words.get(offset + 4)
            if delay is None or offset + 4 >= end:
                failures.add(f"missing-delay-slot:{at(offset)}")
                continue
            if _transfers(delay) or delay == 0x42000018:
                failures.add(f"unsafe-delay-transfer:{at(offset + 4)}")
                continue
            likely = _likely(word)
            unconditional = (op in (4, 20) and rs == rt) or (op == 1 and rs == 0 and rt in (1, 3, 17, 19))
            linking = op == 3 or (indirect and link_register == 31) or (op == 1 and rt in (16, 17, 18, 19))
            taken = replace(frame, ra=False) if linking else frame
            if branch and not unconditional and likely:
                taken = replace(taken, guarded=True)
            delayed = execute(offset + 4, taken)
            if linking and delayed is not None:
                delayed = replace(
                    delayed, saved=tuple((slot, value) for slot, value in delayed.saved if slot >= delayed.sp)
                )
            if branch:
                immediate = word & 65535
                immediate -= 65536 if immediate & 32768 else 0
                target = offset + 4 + immediate * 4
                if delayed is not None:
                    taken = replace(delayed, guarded=delayed.guarded or not unconditional)
                    pending.append((offset + 8 if linking else target, taken))
                if not unconditional:
                    untaken = replace(frame, ra=False) if likely and linking else frame if likely else delayed
                    if untaken is not None:
                        pending.append((offset + 8, replace(untaken, guarded=True)))
            elif delayed is not None:
                if op == 2:
                    target = (((offset + bias + 4) & 0xF0000000) | ((word & 0x3FFFFFF) << 2)) - bias
                    if start <= target < end:
                        pending.append((target, delayed))
                    elif target in known:
                        terminal(offset, delayed, delayed, target)
                    else:
                        failures.add(f"merge-or-unresolved-tail:{at(target)}")
                elif nonlinking:
                    if rs == 31:
                        terminal(offset, frame, delayed)
                    elif jump_tables and jump_tables.get(offset):
                        targets = jump_tables[offset]
                        if any(not start <= target < end or target % 4 for target in targets):
                            failures.add(f"unresolved-local-table-edge:{at(offset)}")
                        else:
                            pending.extend((target, delayed) for target in targets)
                            tags.add("proved-local-jump-table")
                    else:
                        failures.add(f"unresolved-indirect-jump-table-ownership:{at(offset)}")
                else:
                    pending.append((offset + 8, delayed))
        elif word == 0x42000018:
            tags.add("vector-eret")
        else:
            after = execute(offset, frame)
            if after is not None:
                pending.append((offset + 4, after))
    return seen, tuple(sorted(tags)), tuple(sorted(failures))


def evidence(
    words: dict[int, int],
    start: int,
    end: int,
    bias: int,
    sources: set[str],
    known: set[int],
    compiler_shape: Shape,
    jump_tables: dict[int, tuple[int, ...]] | None = None,
) -> Boundary:
    alignment = compiler_shape.object_alignment
    seen, tags, failures = closure(words, start, end, bias, known, jump_tables)
    reasons = list(failures)
    from unbake.work.shape import filler

    leading = filler(
        [words[offset] for offset in range(start, end, 4) if offset in words], start + bias, compiler_shape
    )
    if leading:
        reasons.append(f"compiler-alignment-before-entry:0x{start + bias + leading * 4:X}")
    result = [*sorted(sources), *shape(words, start, end), *tags]
    if not sources:
        reasons.append("entry-source-missing")
    trailing = max(seen, default=start - 4) + 4
    padding = set(range(trailing, end, 4))
    if (
        padding
        and "filler" in compiler_shape.rules
        and alignment > 0
        and alignment & (alignment - 1) == 0
        and (
            (trailing + bias + alignment - 1) // alignment * alignment == end + bias
            and (
                all(words.get(offset) == 0 for offset in padding)
                or _never_starts_c([words[offset] for offset in sorted(padding)], compiler_shape)
            )
        )
    ):
        result.append(f"alignment-padding:{end - trailing}")
    else:
        padding = set()
    dead = {offset for offset in range(start, trailing, 4) if offset not in seen and words.get(offset) == 0}
    if dead:
        result.append("proved-dead-zero-island:" + ",".join(f"0x{offset + bias:X}" for offset in sorted(dead)))
    copies = set()
    if "likely_copy" in compiler_shape.rules:
        # An optimizing compiler can move the target's first instruction into a likely delay slot,
        # retarget the branch past it, and retain the now unreachable original.
        # It belongs only to that exact in-body branch/target pair, never an
        # independently nominated entry or an arbitrary unreachable instruction.
        for offset in seen:
            word = words[offset]
            if not _likely(word) or offset + 4 not in seen:
                continue
            immediate = word & 65535
            immediate -= 65536 if immediate & 32768 else 0
            target = offset + 4 + immediate * 4
            duplicate = target - 4
            if (
                start < duplicate < trailing
                and target in seen
                and duplicate not in seen
                and duplicate not in known
                and words.get(duplicate) == words[offset + 4]
            ):
                copies.add(duplicate)
        if copies:
            result.append("proved-dead-likely-copy:" + ",".join(f"0x{offset + bias:X}" for offset in sorted(copies)))
    uncovered = set(range(start, end, 4)) - seen - padding - dead - copies
    if uncovered:
        reasons.append(
            "unowned-code-or-data-island:" + ",".join(f"0x{offset + bias:X}" for offset in sorted(uncovered))
        )
    else:
        result.append("gapless-partition")
    return Boundary(start, end, tuple(result), tuple(reasons))
