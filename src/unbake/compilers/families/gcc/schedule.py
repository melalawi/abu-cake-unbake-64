"""Read the final RTL order and delay-slot groups from GCC scheduling dumps."""

import re
from collections.abc import Mapping
from pathlib import Path

from unbake.compilers.families.types import Instruction, Schedule
from unbake.config import Held

_INSTRUCTION = r"\((insn|jump_insn|call_insn)(?:/[a-z]+)*(?::[A-Z][A-Z0-9]*)?\s+(\d+)\b"


def _text(value: str | Path, name: str) -> str:
    if isinstance(value, Path):
        try:
            return value.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as error:
            raise Held("schedule", f"{name}: {error}") from error
    if not isinstance(value, str) or not value.strip():
        raise Held("schedule", f"{name} is required")
    return value


def _forms(text: str, name: str) -> list[tuple[int, int, str]]:
    """Retain balanced RTL forms, including quoted parentheses and escapes."""
    stack, quoted, escaped = [], False, False
    forms = []
    for index, char in enumerate(text):
        if quoted:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
        elif char == '"':
            quoted = True
        elif char == "(":
            stack.append(index)
        elif char == ")":
            if not stack:
                raise Held("schedule", f"{name}: unbalanced RTL at character {index}")
            start = stack.pop()
            forms.append((start, index + 1, text[start : index + 1]))
    if stack or quoted:
        raise Held("schedule", f"{name}: truncated RTL")
    return sorted(forms)


def _read(text: str, name: str) -> tuple[tuple[Instruction, ...], tuple[tuple[int, ...], ...]]:
    instructions, sequences = [], []
    forms = _forms(text, name)
    slots = [(start, end) for start, end, rtl in forms if re.match(r"\(sequence\b", rtl)]
    standalone = []
    for start, _end, rtl in forms:
        match = re.match(_INSTRUCTION, rtl)
        if match:
            instructions.append(Instruction(int(match[2]), match[1], text.count("\n", 0, start) + 1, rtl))
            if not re.search(r"\(sequence\b", rtl) and not any(first < start < last for first, last in slots):
                standalone.append(int(match[2]))
        elif re.match(r"\((?:insn|jump_insn|call_insn)\b", rtl):
            raise Held("schedule", f"{name}: instruction.uid is required")
        if re.match(r"\(sequence\b", rtl):
            members = tuple(int(match[2]) for match in re.finditer(_INSTRUCTION, rtl))
            if len(members) < 2:
                raise Held("schedule", f"{name}: sequence.delay_slot is required")
            sequences.append(members)
    # A delay-slot wrapper carries an outer UID but is not an emitted instruction.
    emitted = tuple(item for item in instructions if not re.search(r"\(sequence\b", item.rtl))
    if not emitted:
        raise Held("schedule", f"{name}: instructions are required")
    # GCC reorg can copy an instruction into several delay sequences while
    # retaining its UID. Its dump line identifies each emitted occurrence.
    if len(set(standalone)) != len(standalone):
        raise Held("schedule", f"{name}: duplicate instruction.uid")
    return emitted, tuple(sequences)


def schedule(dumps: Mapping[str, str | Path] | None) -> Schedule:
    """Accept explicit sched2/dbr text or Paths; refuse absent or corrupt evidence."""
    if not isinstance(dumps, Mapping):
        raise Held("schedule", "dumps is required as a mapping")
    values = []
    for stage in ("sched2", "dbr"):
        if stage not in dumps:
            raise Held("schedule", f"dumps.{stage} is required")
        values.append(_read(_text(dumps[stage], f"dumps.{stage}"), f"dumps.{stage}"))
    return Schedule("gcc", True, values[0][0], values[1][0], values[1][1], None)
