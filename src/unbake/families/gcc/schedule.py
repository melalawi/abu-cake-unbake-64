"""Read the final RTL order and delay-slot groups from GCC scheduling dumps."""

import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from unbake.project.config import Held


@dataclass(frozen=True)
class Instruction:
    uid: int
    kind: str
    line: int
    rtl: str


@dataclass(frozen=True)
class Schedule:
    family: str
    available: bool
    sched2: tuple[Instruction, ...]
    dbr: tuple[Instruction, ...]
    delay_slots: tuple[tuple[int, ...], ...]
    reason: str | None


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
    for start, _end, rtl in _forms(text, name):
        match = re.match(r"\((insn|jump_insn|call_insn)(?:/[a-z]+)?\s+(\d+)\b", rtl)
        if match:
            instructions.append(Instruction(int(match[2]), match[1], text.count("\n", 0, start) + 1, rtl))
        elif re.match(r"\((?:insn|jump_insn|call_insn)\b", rtl):
            raise Held("schedule", f"{name}: instruction.uid is required")
        if re.match(r"\(sequence\b", rtl):
            members = tuple(
                int(uid) for uid in re.findall(r"\((?:insn|jump_insn|call_insn)(?:/[a-z]+)?\s+(\d+)\b", rtl)
            )
            if len(members) < 2:
                raise Held("schedule", f"{name}: sequence.delay_slot is required")
            sequences.append(members)
    # A delay-slot wrapper carries an outer UID but is not an emitted instruction.
    emitted = tuple(item for item in instructions if not re.search(r"\(sequence\b", item.rtl))
    if not emitted:
        raise Held("schedule", f"{name}: instructions are required")
    if len({item.uid for item in emitted}) != len(emitted):
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
