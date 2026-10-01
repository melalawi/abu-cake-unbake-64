"""Boundary evidence shared by loaded layout creation and extracted-text audit."""

from __future__ import annotations

import struct
from dataclasses import dataclass

from unbake.layout import boundary_signatures
from unbake.layout.boundary_signatures import Signature


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


def closure(
    words: dict[int, int],
    start: int,
    end: int,
    bias: int,
    known: set[int],
    jump_tables: dict[int, tuple[int, ...]] | None = None,
) -> tuple[set[int], tuple[str, ...], tuple[str, ...]]:
    """Follow a single entry; a jump to another entry is a tail call only after frame restore."""
    pending = [start]
    seen: set[int] = set()
    tags: set[str] = set()
    failures: set[str] = set()
    framed = bool(words.get(start, 0) & 0xFFFF0000 in (0x27BD0000, 0x67BD0000) and words[start] & 0x8000)
    from unbake.layout.split_analysis import instruction

    while pending:
        offset = pending.pop()
        if offset in seen:
            continue
        if not start <= offset < end:
            failures.add(f"merge-or-fallthrough:0x{offset + bias:X}")
            continue
        word = words.get(offset)
        if word is None or not instruction(word):
            failures.add(f"data-or-missing-instruction:0x{offset + bias:X}")
            continue
        seen.add(offset)
        op = word >> 26
        indirect = op == 0 and word & 63 in (8, 9)
        branch = op in (1, 4, 5, 6, 7, 20, 21, 22, 23) or (op == 17 and word >> 21 & 31 == 8)
        if op in (2, 3) or branch or indirect:
            delay = words.get(offset + 4)
            if delay is None or offset + 4 >= end:
                failures.add("missing-delay-slot")
                continue
            if not instruction(delay):
                failures.add(f"invalid-delay-instruction:0x{offset + bias + 4:X}")
                continue
            seen.add(offset + 4)
            if branch:
                displacement = word & 65535
                displacement -= 65536 if displacement & 32768 else 0
                target = offset + 4 + displacement * 4
                if op == 1 and word >> 16 & 31 in (16, 17, 18, 19):
                    pending.append(offset + 8)
                else:
                    pending.append(target)
                    if not (op == 4 and word >> 21 & 31 == word >> 16 & 31):
                        pending.append(offset + 8)
            elif op == 2:
                target = (((offset + bias + 4) & 0xF0000000) | ((word & 0x3FFFFFF) << 2)) - bias
                if start <= target < end:
                    pending.append(target)
                elif target in known and (
                    not framed or (delay & 0xFFFF0000 == 0x27BD0000 and 0 < delay & 65535 < 32768)
                ):
                    tags.add(f"tail-call:0x{target + bias:X}")
                else:
                    failures.add(f"merge-or-unresolved-tail:0x{target + bias:X}")
            elif indirect and word & 63 == 8:
                if word == 0x03E00008:
                    tags.add("control-flow-closed-return")
                    if framed and not any(
                        restored & 0xFFFF0000 in (0x27BD0000, 0x67BD0000) and 0 < restored & 65535 < 32768
                        for restored in (delay, words.get(offset - 4, 0), words.get(offset - 8, 0))
                    ):
                        failures.add("compiler-frame-restore-missing")
                elif jump_tables and offset in jump_tables:
                    pending.extend(jump_tables[offset])
                    tags.add("proved-local-jump-table")
                else:
                    failures.add("unresolved-indirect-jump-table-ownership")
            else:
                pending.append(offset + 8)
        elif op == 0 and word & 63 in (12, 13):
            failures.add("trap-termination")
        elif word == 0x42000018:
            tags.add("vector-eret")
        else:
            pending.append(offset + 4)
    return seen, tuple(sorted(tags)), tuple(sorted(failures))


def evidence(
    words: dict[int, int],
    start: int,
    end: int,
    bias: int,
    sources: set[str],
    known: set[int],
    alignment: int,
    jump_tables: dict[int, tuple[int, ...]] | None = None,
) -> Boundary:
    seen, tags, failures = closure(words, start, end, bias, known, jump_tables)
    reasons = list(failures)
    result = [*sorted(sources), *shape(words, start, end), *tags]
    if not sources:
        reasons.append("entry-source-missing")
    trailing = max(seen, default=start - 4) + 4
    padding = set(range(trailing, end, 4))
    if (
        padding
        and alignment > 0
        and alignment & (alignment - 1) == 0
        and (
            (trailing + bias + alignment - 1) // alignment * alignment == end + bias
            and all(words.get(offset) == 0 for offset in padding)
        )
    ):
        result.append(f"alignment-padding:{end - trailing}")
    else:
        padding = set()
    uncovered = set(range(start, end, 4)) - seen - padding
    if uncovered:
        reasons.append(
            "unowned-code-or-data-island:" + ",".join(f"0x{offset + bias:X}" for offset in sorted(uncovered))
        )
    else:
        result.append("gapless-partition")
    return Boundary(start, end, tuple(result), tuple(reasons))
