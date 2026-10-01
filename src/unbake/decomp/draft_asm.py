"""Preserve MIPS control flow while preparing assembly for m2c analysis."""

import re

from unbake.project.config import Held

_TRANSFER = re.compile(r"(?:b|bal|beq|bne|beqz|bnez|bgez|bgtz|blez|bltz|bc[012][ft])(?:l|al|all)?$|j(?:al|r|alr)?$")


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
