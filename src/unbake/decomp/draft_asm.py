"""Preserve MIPS control flow while preparing assembly for m2c analysis."""

import re

from unbake.project.config import Held

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
