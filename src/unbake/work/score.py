"""Compare a linked candidate function with the original ROM bytes, word by word.

Both sides are final machine words (relocations resolved), so equal bytes are an exact match.
Differences are aligned and classified by the MIPS instruction fields that differ.
"""

from __future__ import annotations

import hashlib
import struct
from collections import Counter, defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Any, Literal

from unbake.process import Fault

TYPES = ("register", "order", "immediate", "relocation", "inserted", "missing", "changed")
_REGISTER_FIELDS = ((21, "rs"), (16, "rt"), (11, "rd"))


@dataclass
class Measurement:
    version: str
    available: bool
    identical_words: int | None
    target_words: int
    candidate_words: int | None
    target_words_different: int | None
    inserted_words: int | None
    typed: dict[str, int] | None
    percent: float | None
    fault: Fault | None
    lines: list[str] = field(default_factory=list)
    register_changes: tuple[tuple[int, int, int, int], ...] = ()
    target: tuple[int, ...] = ()
    candidate: tuple[int, ...] = ()
    strict: dict[str, Any] = field(
        default_factory=lambda: {"available": False, "reason": "historical measurement lacks raw extents"}
    )
    provenance: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        import math

        if (
            not isinstance(self.version, str)
            or not self.version
            or type(self.available) is not bool
            or type(self.target_words) is not int
            or self.target_words < 0
        ):
            raise ValueError("measurement.fields: explicit version, availability and target count required")
        counts = (self.identical_words, self.candidate_words, self.target_words_different, self.inserted_words)
        if self.available:
            if any(type(v) is not int or v < 0 for v in counts):
                raise ValueError("measurement.counts: explicit nonnegative counts required")
            assert self.identical_words is not None and self.candidate_words is not None
            if not isinstance(self.typed, dict) or any(type(v) is not int or v < 0 for v in self.typed.values()):
                raise ValueError("measurement.typed: explicit counts required")
            if (
                self.percent is None
                or type(self.percent) not in (int, float)
                or not math.isfinite(self.percent)
                or not 0 <= self.percent <= 100
            ):
                raise ValueError("measurement.percent: finite measured percent required")
            if (
                self.identical_words > self.target_words
                or self.identical_words > self.candidate_words
                or self.target_words_different != self.target_words - self.identical_words
                or self.fault is not None
            ):
                raise ValueError("measurement.counts: inconsistent available measurement")
        elif any(v is not None for v in (*counts, self.typed, self.percent)) or not isinstance(self.fault, Fault):
            raise ValueError("measurement.unavailable: counts and score must be null with a fault")

    @property
    def provenance_valid(self) -> bool:
        import re

        from unbake.compilers.recipe_options import recipe_digest
        from unbake.work.attempts import dependency_record

        proof = self.provenance
        required = (
            "source_sha256",
            "preprocessed_sha256",
            "object_sha256",
            "placed_object_sha256",
            "linked_sha256",
            "target_sha256",
            "recipe_digest",
            "dependency_digest",
            "compiler_pins",
        )
        if any(
            not isinstance(proof.get(key), str) or re.fullmatch(r"[0-9a-f]{64}", proof[key]) is None for key in required
        ):
            return False
        if proof.get("dependency_current") is not True or proof.get("placement_refusals") != []:
            return False
        if proof["linked_sha256"] != self.strict.get("linked_sha256") or proof["target_sha256"] != self.strict.get(
            "target_sha256"
        ):
            return False
        if recipe_digest(proof.get("recipe")) != proof["recipe_digest"] or not proof.get("recipe", {}).get("pins"):
            return False
        try:
            dependencies = dependency_record(proof["dependencies"])
        except (KeyError, TypeError, ValueError):
            return False
        return (
            dependencies.digest == proof["dependency_digest"]
            and dependencies.values.get("dependencies_unknown") is False
            and bool(dependencies.files)
            and bool(proof.get("placement"))
            and bool(proof.get("compile_argv"))
        )

    @property
    def exact(self) -> bool:
        return (
            self.available
            and self.provenance_valid
            and self.strict.get("available") is True
            and self.strict.get("target_bytes", 0) > 0
            and self.strict.get("target_bytes") == self.strict.get("candidate_bytes")
            and self.strict.get("positional_bytes") == 0
            and self.strict.get("positional_words") == 0
            and self.strict.get("size_delta") == 0
            and self.strict.get("target_sha256") == self.strict.get("linked_sha256")
            and self.target_words > 0
            and self.target_words_different == 0
            and self.inserted_words == 0
            and not any((self.typed or {}).values())
        )

    def document(self) -> dict[str, object]:
        return {
            "version": self.version,
            "available": self.available,
            "identical_words": self.identical_words,
            "target_words": self.target_words,
            "candidate_words": self.candidate_words,
            "target_words_different": self.target_words_different,
            "inserted_words": self.inserted_words,
            "typed": self.typed,
            "percent": round(self.percent, 6) if self.percent is not None else None,
            "exact": self.exact,
            "fault": self.fault.document() if self.fault else None,
            "strict": self.strict,
            "provenance": self.provenance,
        }

    @classmethod
    def read(cls, value: dict[str, Any]) -> Measurement:
        required = {
            "version",
            "available",
            "identical_words",
            "target_words",
            "candidate_words",
            "target_words_different",
            "inserted_words",
            "typed",
            "percent",
            "fault",
            "strict",
            "provenance",
        }
        if required - value.keys():
            raise ValueError("measurement.fields: complete M10 record required")
        result = cls(
            value["version"],
            value["available"],
            value["identical_words"],
            value["target_words"],
            value["candidate_words"],
            value["target_words_different"],
            value["inserted_words"],
            value["typed"],
            value["percent"],
            Fault.read(value["fault"]) if value["fault"] else None,
        )
        result.strict = value["strict"]
        result.provenance = value["provenance"]
        if result.strict.get("available") is True:
            required_strict = {
                "target_bytes",
                "candidate_bytes",
                "positional_bytes",
                "positional_words",
                "size_delta",
                "target_sha256",
                "linked_sha256",
            }
            if required_strict - result.strict.keys():
                raise ValueError("measurement.strict: complete positional record required")
            if any(
                type(result.strict[k]) is not int or result.strict[k] < 0
                for k in required_strict - {"size_delta", "target_sha256", "linked_sha256"}
            ):
                raise ValueError("measurement.strict: invalid extents/counts")
        return result

    def description(self) -> str:
        if not self.available:
            return f"{self.version}: measurement unavailable"
        different = self.target_words_different
        return (
            f"{self.version}: {self.identical_words}/{self.target_words} words identical; "
            f"{different} target word{'s' if different != 1 else ''} different; {self.inserted_words} inserted words"
        )


def unavailable(version: str, target_words: int, fault: Fault) -> Measurement:
    return Measurement(version, False, None, target_words, None, None, None, None, None, fault)


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


def measure_words(version: str, target: bytes, candidate: bytes) -> Measurement:
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
    result = Measurement(
        version,
        True,
        identical,
        total,
        len(right),
        total - identical,
        typed["inserted"],
        typed,
        percent,
        None,
        lines,
        tuple(register_changes),
        left,
        right,
    )
    # Diagnostic padding above never enters positional raw byte equality.
    result.strict = {
        "available": True,
        "target_bytes": len(target),
        "candidate_bytes": len(candidate),
        "positional_bytes": sum(a != b for a, b in zip(target, candidate, strict=False))
        + abs(len(target) - len(candidate)),
        "positional_words": sum(
            target[i : i + 4] != candidate[i : i + 4] for i in range(0, max(len(target), len(candidate)), 4)
        ),
        "size_delta": len(candidate) - len(target),
        "target_sha256": hashlib.sha256(target).hexdigest(),
        "linked_sha256": hashlib.sha256(candidate).hexdigest(),
    }
    return result


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
