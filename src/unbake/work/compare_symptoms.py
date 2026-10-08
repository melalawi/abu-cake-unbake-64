"""Advisory symptoms from retained measured words; never run diagnostics."""

from __future__ import annotations

from collections import Counter
from difflib import SequenceMatcher
from typing import Any

from unbake.work.score import Measurement, classify


def _signed(value: int) -> int:
    return value if value < 32768 else value - 65536


def symptoms(result: Measurement) -> dict[str, Any]:
    if not result.available:
        return {
            "size_delta_bytes": None,
            "register_dominant": None,
            "sp_offset_only": None,
            "literal_immediates_off_four": None,
            "repeated_deleted_runs": None,
            "frame_delta_bytes": None,
            "float_register_differences": None,
        }
    target, candidate = result.target, result.candidate
    deleted: Counter[tuple[int, ...]] = Counter()
    differences: list[tuple[int, int]] = []
    structural = False
    for tag, a, b, c, d in SequenceMatcher(None, target, candidate, autojunk=False).get_opcodes():
        if tag == "delete":
            deleted[target[a:b]] += 1
        if tag != "equal":
            structural |= b - a != d - c
            if tag == "replace":
                differences.extend(zip(target[a:b], candidate[c:d], strict=False))
    sp_only = bool(differences) and not structural
    off_four = float_regs = 0
    memory = {*range(0x20, 0x2F), 0x31, 0x35, 0x39, 0x3D}
    for left, right in differences:
        kind = classify(left, right)
        op = left >> 26
        same_encoding = left >> 16 == right >> 16
        sp_only &= kind == "immediate" and same_encoding and (left >> 21) & 31 == 29 and op in memory | {9}
        if same_encoding and op in {0x23, 0x31, 0x35} and (left >> 21) & 31 != 29:
            off_four += abs(_signed(left & 65535) - _signed(right & 65535)) == 4
        float_regs += kind == "register" and op in {17, 0x31, 0x35, 0x39, 0x3D}

    def frame(words: tuple[int, ...]) -> int | None:
        # Only a unique entry-window addiu sp,sp,-N establishes this observation.
        sizes = [-_signed(w & 65535) for w in words[:8] if w >> 16 == 0x27BD and _signed(w & 65535) < 0]
        return sizes[0] if len(sizes) == 1 else None

    tf, cf = frame(target), frame(candidate)
    typed = result.typed or {}
    registers = typed.get("register", 0)
    return {
        "size_delta_bytes": result.strict.get("size_delta"),
        "register_dominant": registers > 0
        and registers > typed.get("immediate", 0)
        and not any(typed.get(key, 0) for key in ("changed", "inserted", "missing")),
        "sp_offset_only": sp_only,
        "literal_immediates_off_four": off_four,
        "repeated_deleted_runs": max((count for run, count in deleted.items() if len(run) >= 2), default=0),
        "frame_delta_bytes": cf - tf if cf is not None and tf is not None else None,
        "float_register_differences": float_regs,
        "typed_register": registers,
        "typed_immediate": typed.get("immediate"),
    }
