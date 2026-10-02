"""Align MIPS words and classify draft differences."""

from __future__ import annotations

import struct
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Literal

TYPES = ("register", "order", "immediate", "relocation", "inserted", "missing", "changed")


def align_words(
    target: Sequence[int], candidate: Sequence[int], relocations: dict[int, int]
) -> list[tuple[Literal["replace", "delete", "insert", "equal"], int, int, int, int]]:
    """Align instruction sequences with relocation operands masked on both sides.

    Masks apply only to instruction forms present in the object's relocation records.
    The returned offsets retain the original words for exact identity checks.
    """
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


@dataclass
class Compare:
    version: str
    identical: int
    of: int
    typed: dict[str, int]
    lines: list[str]
    match_percent: float
    register_changes: tuple[tuple[int, int, int, int], ...]
    naming: int = 0
    target_words: tuple[int, ...] = ()
    candidate_words: tuple[int, ...] = ()


def words(data: bytes) -> list[int]:
    return [word[0] for word in struct.iter_unpack(">I", data)]


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


def register_number(name: str) -> int | None:
    """Decode objdiff's MIPS register spelling for allocator diagnostics."""
    general = (
        "zero",
        "at",
        "v0",
        "v1",
        "a0",
        "a1",
        "a2",
        "a3",
        "t0",
        "t1",
        "t2",
        "t3",
        "t4",
        "t5",
        "t6",
        "t7",
        "s0",
        "s1",
        "s2",
        "s3",
        "s4",
        "s5",
        "s6",
        "s7",
        "t8",
        "t9",
        "k0",
        "k1",
        "gp",
        "sp",
        "fp",
        "ra",
    )
    name = name.removeprefix("$")
    if name in general:
        return general.index(name)
    floating = (
        "fv0",
        "fv1",
        "ft0",
        "ft1",
        "ft2",
        "ft3",
        "fa0",
        "fa1",
        "ft4",
        "ft5",
        "fs0",
        "fs1",
        "fs2",
        "fs3",
        "fs4",
        "fs5",
    )
    for index, prefix in enumerate(floating):
        if name in (prefix + "f", prefix):
            return 32 + index * 2
        if name == prefix + "e":
            return 33 + index * 2
    if name.startswith("f") and name[1:].isdigit() and int(name[1:]) < 32:
        return 32 + int(name[1:])
    return None


def compare_object(version: str, document: dict[str, object], function: str) -> Compare:
    """Classify aligned rows using resolved targets, retaining naming debt."""
    from typing import Any, cast

    from unbake.decomp.score import percent

    left = cast(dict[str, Any], document["left"])
    right = cast(dict[str, Any], document["right"])
    target = next(s for s in left["symbols"] if s.get("name") == function and s.get("kind") == "SYMBOL_FUNCTION")
    draft = next(s for s in right["symbols"] if s.get("name") == function and s.get("kind") == "SYMBOL_FUNCTION")
    a, b = target.get("instructions", []), draft.get("instructions", [])
    typed = dict.fromkeys(TYPES, 0)
    identical = 0
    naming = 0
    addresses = cast(dict[str, int], document.get("symbol_addresses", {}))
    sections = cast(dict[str, dict[str, int]], document.get("section_addresses", {}))
    register_changes = []
    details = []
    missing: dict[str, list[int]] = defaultdict(list)
    inserted: dict[str, list[int]] = defaultdict(list)

    def relocation(
        row: dict[str, Any], side: dict[str, Any], entry: dict[str, Any], *, resolved: bool
    ) -> tuple[object, ...] | None:
        instruction = row.get("instruction", {})
        value = instruction.get("relocation")
        start = int(entry.get("address", 0))
        destination = instruction.get("branch_dest")
        # A linker fixup and an already encoded local branch have the same target.
        if resolved and destination is not None and start <= int(destination) < start + int(entry.get("size", 0)):
            return "function-offset", int(destination) - start
        if value is None:
            return None
        symbol = side["symbols"][int(value.get("target_symbol", 0))]
        name, type_, addend = symbol["name"], value.get("type", 0), int(value.get("addend", 0))
        if resolved:
            side_name = "left" if side is left else "right"
            paired = cast(dict[str, dict[int, tuple[int, int]]], document.get("relocation_addresses", {}))
            effective = paired.get(side_name, {}).get(int(instruction.get("address", 0)))
            if effective is not None:
                return "address", *effective
            bases = sections.get(side_name, {})
            if name in bases:
                return "address", type_, bases[name] + addend
            if name in addresses:
                return "address", type_, addresses[name] + addend
        return "name", type_, name, addend

    def operands(instruction: dict[str, Any]) -> list[object]:
        # Normalize only the target argument; registers and opcodes stay exact.
        return [
            {"arg": {"reloc": True}} if "branch_dest" in part.get("arg", {}) else part
            for part in instruction.get("parts", [])
        ]

    for i in range(max(len(a), len(b))):
        x, y = a[i] if i < len(a) else {}, b[i] if i < len(b) else {}
        before, after = x.get("instruction"), y.get("instruction")
        kind = "same"
        if before is None and after is None:
            continue
        if before is None:
            kind = "inserted"
        elif after is None:
            kind = "missing"
        elif relocation(x, left, target, resolved=True) != relocation(y, right, draft, resolved=True):
            kind = "relocation"
        else:
            equalised = relocation(x, left, target, resolved=False) != relocation(y, right, draft, resolved=False)
            naming += int(equalised)
            # Objdiff can flag an equal effective addend as an argument mismatch.
            # Compare the non-relocation parts too, so naming never hides code edits.
            if equalised and operands(before) == operands(after):
                identical += 1
                continue
        if kind == "relocation" and before is not None and after is not None:
            # A changed branch/call/load opcode must not disappear behind its
            # different relocation target. Both differences are evidence.
            opcodes_before = [part["opcode"] for part in before.get("parts", []) if "opcode" in part]
            opcodes_after = [part["opcode"] for part in after.get("parts", []) if "opcode" in part]
            if opcodes_before != opcodes_after:
                typed["changed"] += 1
                details.append("changed: opcode differs at an instruction with a relocation difference")
        if kind == "same" and (
            x.get("diff_kind", "DIFF_NONE") != "DIFF_NONE" or y.get("diff_kind", "DIFF_NONE") != "DIFF_NONE"
        ):
            assert before is not None and after is not None
            if x.get("diff_kind") == "DIFF_ARG_MISMATCH":
                args1 = [p["arg"] for p in before.get("parts", []) if "arg" in p]
                args2 = [p["arg"] for p in after.get("parts", []) if "arg" in p]
                changes = [(u, v) for u, v in zip(args1, args2, strict=False) if u != v]
                register_args = [
                    "opaque" in u
                    and "opaque" in v
                    and register_number(u["opaque"]) is not None
                    and register_number(v["opaque"]) is not None
                    for u, v in changes
                ]
                kind = (
                    "register"
                    if register_args and all(register_args)
                    else "changed"
                    if any(register_args)
                    else "immediate"
                )
            else:
                kind = "changed"
        if kind == "same":
            identical += 1
            continue
        typed[kind] += 1
        if kind == "register":
            assert before is not None and after is not None
            for u, v in changes:
                before_register, after_register = register_number(u["opaque"]), register_number(v["opaque"])
                if before_register is not None and after_register is not None:
                    register_changes.append(
                        (int(before.get("address", 0)), int(after.get("address", 0)), before_register, after_register)
                    )

        def description(instruction: dict[str, Any] | None) -> str:
            return (
                "-"
                if instruction is None
                else f"+0x{int(instruction.get('address', 0)):04X} {instruction.get('formatted', '')}"
            )

        details.append(f"{kind}: target {description(before)}; draft {description(after)}")
        if kind == "missing" and before:
            missing[before.get("formatted", "")].append(len(details) - 1)
        elif kind == "inserted" and after:
            inserted[after.get("formatted", "")].append(len(details) - 1)
    for instruction, positions in missing.items():
        for old, new in zip(positions, inserted[instruction], strict=False):
            typed["missing"] -= 1
            typed["inserted"] -= 1
            typed["order"] += 1
            details[old] = (
                details[old].split("; draft ")[0].replace("missing:", "order:", 1)
                + "; draft "
                + details[new].split("; draft ")[1]
            )
            details[new] = ""
    table_differences = cast(list[str], document.get("jump_table_differences", []))
    typed["relocation"] += len(table_differences)
    details.extend(table_differences)
    total = sum("instruction" in row for row in a)
    match = percent(target.get("match_percent", 0), f"{function}.match_percent", "try")
    lines = [
        f"VERSION {version}: identical {identical} of {total} instructions; objdiff {match:.6f}%",
        "typed: " + ", ".join(f"{kind}={typed[kind]}" for kind in TYPES),
    ]
    if naming:
        lines.append(f"naming: {naming} relocations equalised by address")
    details = [line for line in details if line]
    if details:
        lines.append("first divergence: " + details[0])
        lines.extend(details)
    return Compare(version, identical, total, typed, lines, match, tuple(register_changes), naming)
