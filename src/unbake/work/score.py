"""Compare a linked candidate function with the original ROM bytes, word by word.

Both sides are final machine words (relocations resolved), so equal bytes are an exact match.
Differences are aligned and classified by the MIPS instruction fields that differ.
"""

from __future__ import annotations

import struct
from collections import Counter, defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Literal

TYPES = ("register", "order", "immediate", "relocation", "inserted", "missing", "changed")
_REGISTER_FIELDS = ((21, "rs"), (16, "rt"), (11, "rd"))


@dataclass
class Compare:
    version: str
    identical: int
    of: int
    typed: dict[str, int]
    lines: list[str]
    match_percent: float
    register_changes: tuple[tuple[int, int, int, int], ...] = ()
    target_words: tuple[int, ...] = ()
    candidate_words: tuple[int, ...] = ()

    @property
    def exact(self) -> bool:
        return self.of > 0 and self.identical == self.of and not any(self.typed.values())

    def document(self) -> dict[str, object]:
        return {
            "percent": round(self.match_percent, 6),
            "exact": self.exact,
            "identical": self.identical,
            "of": self.of,
            "typed": dict(self.typed),
        }


def words(data: bytes) -> tuple[int, ...]:
    if len(data) % 4:
        data = data + bytes(-len(data) % 4)
    return tuple(word for (word,) in struct.iter_unpack(">I", data))


def fields(word: int) -> tuple[int, int]:
    """(register mask, immediate mask) for MIPS III instruction forms."""
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
    if opcode == 1 or opcode in (6, 7, 0x16, 0x17):
        return 0x03E00000, 0xFFFF
    if opcode == 0x0F:
        return 0x001F0000, 0xFFFF
    if opcode in (4, 5, *range(8, 15), 0x14, 0x15, 0x18, 0x19, *range(0x20, 0x40)):
        return 0x03FF0000, 0xFFFF
    return 0, 0


def classify(target: int, candidate: int) -> str:
    """The kind of difference between two aligned words."""
    registers, immediate = fields(target)
    difference = target ^ candidate
    if difference & ~(registers | immediate) & 0xFFFFFFFF:
        return "changed"
    if difference & registers and difference & immediate:
        return "changed"
    return "register" if difference & registers else "immediate"


def _register_changes(offset_t: int, offset_c: int, target: int, candidate: int) -> list[tuple[int, int, int, int]]:
    changes = []
    for shift, _ in _REGISTER_FIELDS:
        before, after = (target >> shift) & 31, (candidate >> shift) & 31
        if before != after:
            changes.append((offset_t * 4, offset_c * 4, before, after))
    return changes


def compare_words(version: str, target: bytes, candidate: bytes) -> Compare:
    """Align two word sequences and count identical and typed differences."""
    left, right = words(target), words(candidate)
    typed = dict.fromkeys(TYPES, 0)
    details: list[str] = []
    register_changes: list[tuple[int, int, int, int]] = []
    identical = 0
    missing: Counter[int] = Counter()
    inserted: Counter[int] = Counter()
    for tag, i1, i2, j1, j2 in SequenceMatcher(None, left, right, autojunk=False).get_opcodes():
        if tag == "equal":
            identical += i2 - i1
            continue
        pairs = min(i2 - i1, j2 - j1) if tag == "replace" else 0
        for offset in range(pairs):
            kind = classify(left[i1 + offset], right[j1 + offset])
            typed[kind] += 1
            if kind == "register":
                register_changes.extend(
                    _register_changes(i1 + offset, j1 + offset, left[i1 + offset], right[j1 + offset])
                )
            details.append(
                f"{kind}: target +0x{(i1 + offset) * 4:04X} {left[i1 + offset]:08X}; "
                f"candidate +0x{(j1 + offset) * 4:04X} {right[j1 + offset]:08X}"
            )
        for index in range(i1 + pairs, i2):
            missing[left[index]] += 1
            details.append(f"missing: target +0x{index * 4:04X} {left[index]:08X}")
        for index in range(j1 + pairs, j2):
            inserted[right[index]] += 1
            details.append(f"inserted: candidate +0x{index * 4:04X} {right[index]:08X}")
    moved = sum((missing & inserted).values())
    typed["missing"] = sum(missing.values()) - moved
    typed["inserted"] = sum(inserted.values()) - moved
    typed["order"] = moved
    total = len(left)
    percent = 100.0 * identical / max(len(left), len(right), 1)
    lines = [
        f"VERSION {version}: identical {identical} of {total} words ({percent:.2f}%)",
        "typed: " + ", ".join(f"{kind}={typed[kind]}" for kind in TYPES),
    ]
    if details:
        lines.append("first divergence: " + details[0])
    return Compare(version, identical, total, typed, lines, percent, tuple(register_changes), left, right)


def weakest(scores: dict[str, float]) -> float:
    return min(scores.values()) if scores else 0.0


def align_words(
    target: Sequence[int], candidate: Sequence[int], relocations: dict[int, int]
) -> list[tuple[Literal["replace", "delete", "insert", "equal"], int, int, int, int]]:
    """Align instruction sequences with the candidate's relocated operand fields masked on both sides."""
    forms: defaultdict[int, set[int]] = defaultdict(set)
    for offset, mask in relocations.items():
        if 0 <= offset < len(candidate):
            forms[mask].add(candidate[offset] & ~mask)

    def key(word: int) -> int:
        for mask, instructions in forms.items():
            if word & ~mask in instructions:
                return word & ~mask
        return word

    return SequenceMatcher(None, [key(w) for w in target], [key(w) for w in candidate], autojunk=False).get_opcodes()
