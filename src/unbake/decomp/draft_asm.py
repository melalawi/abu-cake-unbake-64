"""Preserve MIPS control flow while preparing assembly for m2c analysis."""

import re

from unbake.config import Held

_TRANSFER = re.compile(r"(?:b|bal|beq|bne|beqz|bnez|bgez|bgtz|blez|bltz|bc[012][ft])(?:l|al|all)?$|j(?:al|r|alr)?$")


def saved_returns(assembly: str) -> str:
    """Recognize a return through an unchanged entry copy of the saved RA."""
    clean = re.sub(r"/\*.*?\*/|#[^\n]*", "", assembly, flags=re.S)
    instructions = [line.strip() for line in clean.splitlines() if re.match(r"\s*[A-Za-z][\w.]*\s+\$", line)]
    if not instructions:
        return assembly
    copied = re.fullmatch(
        r"(?:addu|or)\s+\$(s[0-7]|fp),\s*\$ra,\s*\$zero|move\s+\$(s[0-7]|fp),\s*\$ra", instructions[0]
    )
    if copied is None:
        return assembly
    register = copied[1] or copied[2]
    writes = [
        line
        for line in instructions
        if re.match(r"[A-Za-z][\w.]*\s+\$" + register + r"\s*,", line) and not re.match(r"(?:sw|sd|sh|sb)\s", line)
    ]
    if len(writes) != 1:
        return assembly
    return re.sub(r"\bjr\s+\$" + register + r"\b", "jr $ra", assembly)


def local_targets(assembly: str) -> str:
    """Recover omitted local labels only at an existing measured instruction."""
    labels = set(re.findall(r"^\s*(?:[ga]label\s+([\w.$]+)|([\w.$]+):)\s*$", assembly, re.M))
    defined = {name for pair in labels for name in pair if name}
    referenced = set()
    for line in assembly.splitlines():
        clean = re.sub(r"/\*.*?\*/|#[^\n]*", "", line).strip()
        words = clean.split()
        if words and _TRANSFER.fullmatch(words[0]) and words[0] not in ("jal", "jalr", "jr"):
            referenced.add(words[-1])
    for name in sorted(referenced - defined):
        numeric = re.fullmatch(r"\.L([0-9A-Fa-f]{8})(?:_auto)?", name)
        if numeric is None:
            continue
        pattern = re.compile(r"^(\s*/\*\s*[0-9A-Fa-f]+\s+" + numeric[1] + r"\s+[^\n]+)$", re.M | re.I)
        matches = list(pattern.finditer(assembly))
        if len(matches) == 1:
            assembly = assembly[: matches[0].start()] + name + ":\n" + assembly[matches[0].start() :]
    return assembly


def delay_slots(assembly: str, function: str) -> str:
    """Split labeled slots into separate delay and direct-entry copies.

    A branch's fallthrough skips the direct-entry copy; a branch-likely still
    annuls its original slot. Jump targets execute the direct-entry copy once.
    The transformation is confined to analysis input, never extracted objects.
    """
    lines = assembly.splitlines(keepends=True)
    instructions: list[tuple[int, str]] = []
    for index, line in enumerate(lines):
        clean = re.sub(r"/\*.*?\*/|#.*", "", line).strip()
        if clean and re.fullmatch(r"[A-Za-z][\w.]*", clean.split()[0]):
            if clean.split()[0] in ("glabel", "alabel", "endlabel", "nonmatching"):
                continue
            instructions.append((index, clean))
    if instructions and _TRANSFER.fullmatch(instructions[-1][1].split()[0]):
        raise Held(
            "m2c", f"{function}: split assembly ends with a transfer missing its delay slot: {instructions[-1][1]}"
        )
    for ordinal in range(len(instructions) - 1, 0, -1):
        previous, branch = instructions[ordinal - 1]
        index, slot = instructions[ordinal]
        if not _TRANSFER.fullmatch(branch.split()[0]):
            continue
        if _TRANSFER.fullmatch(slot.split()[0]):
            raise Held("m2c", f"{function}: control transfer in a delay slot: {slot}")
        between = lines[previous + 1 : index]
        if not any(re.fullmatch(r"\s*(?:[\w.$]+:|(?:glabel|alabel)\s+[\w.$]+)\s*", line) for line in between):
            continue
        label = f".L_unbake_after_slot_{ordinal}"
        while label in assembly:
            label += "_"
        lines[previous + 1 : index + 1] = [
            f"    {slot}\n",
            f"    b {label}\n",
            "    nop\n",
            *between,
            lines[index],
            label + ":\n",
        ]
    return "".join(lines)


def address_aliases(assembly: str, values: dict[str, int]) -> str:
    """Give both halves of an unaligned transfer the same resolved symbol.

    m2c pairs symbolic loads syntactically. Adjacent splat aliases can name
    the two bytes of one transfer differently, despite identical addresses.
    Only relocations at the measured left address or its right byte change.
    """
    expression = re.compile(r"([A-Za-z_]\w*)(?:\s*([+-])\s*(0[xX][\da-fA-F]+|\d+))?")

    def address(text: str) -> int | None:
        match = expression.fullmatch(text.strip())
        if match is None or match[1] not in values:
            return None
        offset = int(match[3], 0) if match[3] else 0
        return values[match[1]] + (-offset if match[2] == "-" else offset)

    lefts: dict[int, str] = {}
    for match in re.finditer(r"\b(?:lwl|swl)\s+\$\w+,\s*%lo\(([^)]+)\)", assembly):
        resolved = address(match[1])
        if resolved is not None:
            lefts.setdefault(resolved, match[1].strip())

    def replace(match: re.Match[str]) -> str:
        resolved = address(match[2])
        if resolved in lefts:
            return f"%{match[1]}({lefts[resolved]})"
        if resolved is not None and resolved - 3 in lefts:
            origin = expression.fullmatch(lefts[resolved - 3])
            assert origin is not None
            delta = (int(origin[3], 0) * (-1 if origin[2] == "-" else 1) if origin[3] else 0) + 3
            tail = f" {'-' if delta < 0 else '+'} 0x{abs(delta):X}" if delta else ""
            return f"%{match[1]}({origin[1]}{tail})"
        return match[0]

    assembly = re.sub(r"%(hi|lo)\(([^)]+)\)", replace, assembly)
    lines = assembly.splitlines(keepends=True)
    for index, line in enumerate(lines):
        transfer = re.search(r"\b(lwl|lwr|swl|swr)\s+\$\w+,\s*%lo\(([^)]+)\)\((\$\w+)\)", line)
        if transfer is None:
            continue
        resolved = address(transfer[2])
        delta = 3 if transfer[1].endswith("r") else 0
        if resolved is None or resolved - delta not in lefts:
            continue
        origin = lefts[resolved - delta]
        # m2c treats symbolic %hi as the complete address and %lo as zero.
        # Retain the byte delta explicitly in this analysis-only input.
        for previous in range(index - 1, max(-1, index - 4), -1):
            high = re.search(r"\blui\s+" + re.escape(transfer[3]) + r",\s*%hi\(([^)]+)\)", lines[previous])
            if high and address(high[1]) == resolved:
                lines[previous] = lines[previous][: high.start(1)] + origin + lines[previous][high.end(1) :]
                lines[index] = line[: transfer.start(2) - 4] + str(delta) + line[transfer.end(2) + 1 :]
                break
    return "".join(lines)
