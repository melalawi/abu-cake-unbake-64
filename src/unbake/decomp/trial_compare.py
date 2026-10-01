"""Align MIPS words and classify draft differences."""

from __future__ import annotations

import struct
from collections import Counter, defaultdict, deque
from dataclasses import dataclass
from difflib import SequenceMatcher

TYPES = ("register", "order", "immediate", "relocation", "inserted", "missing", "changed")


@dataclass
class Compare:
    version: str
    identical: int
    of: int
    typed: dict[str, int]
    lines: list[str]


def fields(word: int) -> tuple[int, int]:
    """Masks for register and immediate operands in MIPS III instructions."""
    opcode = word >> 26
    if opcode in (2, 3):
        return 0, 0x03FFFFFF
    if opcode == 0:
        function = word & 63
        if function in (0, 2, 3, 0x38, 0x3A, 0x3B, 0x3C, 0x3E, 0x3F):
            return 0x001FF800, 0x7C0
        if function in (0x0C, 0x0D):
            return 0, 0x03FFFFC0
        return 0x03FFF800, 0
    if opcode in (0x10, 0x11, 0x12):
        mode = (word >> 21) & 31
        if mode == 8:
            return 0, 0xFFFF
        if mode in (0, 1, 2, 4, 5, 6):
            return 0x001FF800, 0
        if opcode == 0x11 and mode >= 16:
            return 0x001FFFC0, 0
        return 0, 0
    if opcode == 1:
        return 0x03E00000, 0xFFFF
    if opcode in (6, 7, 0x16, 0x17):
        return 0x03E00000, 0xFFFF
    if opcode == 0x0F:
        return 0x001F0000, 0xFFFF
    if opcode in (4, 5, *range(8, 15), 0x14, 0x15, 0x18, 0x19, *range(0x20, 0x40)):
        return 0x03FF0000, 0xFFFF
    return 0, 0


def classify(target: int, candidate: int, relocated: int) -> str:
    difference = target ^ candidate
    if not difference:
        return "same"
    if relocated and not difference & ~relocated:
        return "relocation"
    registers, immediate = fields(target)
    candidate_registers, candidate_immediate = fields(candidate)
    if (registers, immediate) != (candidate_registers, candidate_immediate):
        return "changed"
    if registers and not difference & ~registers:
        return "register"
    if immediate and not difference & ~immediate:
        return "immediate"
    return "changed"


def compare_words(
    version: str, target: list[int], candidate: list[int], relocations: dict[int, int] | None = None
) -> Compare:
    relocations = {} if relocations is None else relocations
    operations: list[tuple[str, int | None, int | None]] = []

    def align(left: int, right: int, start: int, stop: int, shape: bool) -> None:
        def key(word: int) -> int:
            if not shape:
                return word
            registers, immediate = fields(word)
            return word & ~(registers | immediate)

        matcher = SequenceMatcher(
            None, [key(w) for w in target[left:right]], [key(w) for w in candidate[start:stop]], autojunk=False
        )
        for tag, a, b, c, d in matcher.get_opcodes():
            a, b, c, d = a + left, b + left, c + start, d + start
            if tag == "equal":
                operations.extend(
                    (classify(target[i], candidate[j], relocations.get(j, 0)), i, j)
                    for i, j in zip(range(a, b), range(c, d), strict=False)
                )
            elif tag == "replace" and not shape:
                align(a, b, c, d, True)
            else:
                paired = min(b - a, d - c) if tag == "replace" else 0
                operations.extend(
                    (classify(target[a + k], candidate[c + k], relocations.get(c + k, 0)), a + k, c + k)
                    for k in range(paired)
                )
                operations.extend(("missing", i, None) for i in range(a + paired, b))
                operations.extend(("inserted", None, j) for j in range(c + paired, d))

    align(0, len(target), 0, len(candidate), False)
    extras: defaultdict[int, deque[int]] = defaultdict(deque)
    for position, (kind, _, j) in enumerate(operations):
        if kind == "inserted" and j is not None:
            extras[candidate[j]].append(position)
    removed = set()
    for position, (kind, i, _) in enumerate(operations):
        if kind == "missing" and i is not None and extras[target[i]]:
            extra = extras[target[i]].popleft()
            operations[position] = ("order", i, operations[extra][2])
            removed.add(extra)
    operations = [op for position, op in enumerate(operations) if position not in removed]
    if len(target) == len(candidate):
        positional: list[tuple[str, int | None, int | None]] = [
            (classify(a, b, relocations.get(i, 0)), i, i)
            for i, (a, b) in enumerate(zip(target, candidate, strict=False))
        ]
        if sum(op[0] == "same" for op in positional) > sum(op[0] == "same" for op in operations):
            operations = positional
    counts = Counter(kind for kind, _, _ in operations)
    typed = {kind: counts[kind] for kind in TYPES}
    lines = [
        f"{version}: identical {counts['same']} of {len(target)} words",
        "typed: " + ", ".join(f"{kind}={typed[kind]}" for kind in TYPES),
    ]
    for kind, i, j in operations:
        if kind == "same":
            continue
        before = "-" if i is None else f"+0x{i * 4:04X} {target[i]:08X}"
        after = "-" if j is None else f"+0x{j * 4:04X} {candidate[j]:08X}"
        lines.append(f"{kind}: target {before}; draft {after}")
    return Compare(version, counts["same"], len(target), typed, lines)


def words(data: bytes) -> list[int]:
    return [word[0] for word in struct.iter_unpack(">I", data)]
