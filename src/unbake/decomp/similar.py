"""Retrieve landed C and assembly by local opcode sequence similarity."""

from __future__ import annotations

import re
import struct
from dataclasses import dataclass
from pathlib import Path

from unbake.config import Held, Project
from unbake.fold.drafts import is_partial
from unbake.layout import split
from unbake.work.score import fields


@dataclass(frozen=True)
class Similar:
    function: str
    version: str
    edit_distance: int
    distance: float
    overlap: float
    source: Path
    c: str
    asm: str


def opcodes(data: bytes) -> tuple[int, ...]:
    """Discard operands using the shared MIPS III instruction masks."""
    if not data or len(data) % 4:
        raise Held("similar", "opcodes require nonempty complete big-endian words")
    result = []
    for (word,) in struct.iter_unpack(">I", data):
        registers, immediates = fields(word)
        result.append(word & ~(registers | immediates))
    return tuple(result)


def levenshtein(left: tuple[int, ...], right: tuple[int, ...], bound: int) -> int:
    """Banded edit distance; return bound + 1 when the distance exceeds it."""
    if bound < 0:
        raise Held("similar", "distance bound must be nonnegative")
    if abs(len(left) - len(right)) > bound:
        return bound + 1
    if len(left) < len(right):
        left, right = right, left
    previous = {index: index for index in range(min(len(right), bound) + 1)}
    infinity = bound + 1
    for row, token in enumerate(left, 1):
        current = {0: row} if row <= bound else {}
        for column in range(max(1, row - bound), min(len(right), row + bound) + 1):
            current[column] = min(
                previous.get(column, infinity) + 1,
                current.get(column - 1, infinity) + 1,
                previous.get(column - 1, infinity) + (token != right[column - 1]),
            )
        if min(current.values(), default=infinity) > bound:
            return infinity
        previous = current
    return min(previous.get(len(right), infinity), infinity)


def overlap(left: tuple[int, ...], right: tuple[int, ...]) -> float:
    """Jaccard overlap of opcode trigrams, including short functions."""
    width = min(3, len(left), len(right))
    if not width:
        return 0.0
    a = {left[index : index + width] for index in range(len(left) - width + 1)}
    b = {right[index : index + width] for index in range(len(right) - width + 1)}
    return len(a & b) / len(a | b)


def retrieve(
    project: Project, function: str, version: str, extracted: Path, *, top_k: int = 5, bound: int = 512
) -> list[Similar]:
    """Rank matched rows in one VERSION; skip absent and partial sources.

    Distances are exact for returned rows. Candidates beyond the edit bound
    are excluded, bounding work without truncating either opcode sequence.
    """
    if not isinstance(function, str) or not re.fullmatch(r"[A-Za-z_]\w*", function):
        raise Held("similar", "function must be a C identifier")
    if top_k <= 0 or bound < 0:
        raise Held("similar", "top_k must be positive and bound nonnegative")
    functions = split.functions(project, version)
    targets = [item for item in functions if function in (item.name, *item.aliases)]
    if len(targets) != 1:
        raise Held("similar", f"{function}: expected one function row in VERSION {version}, found {len(targets)}")
    target = targets[0]
    tokens = opcodes(split.words(project, target))
    names = {target.name, *target.aliases}
    ranked: list[tuple[float, float, str, int, Path, str, split.Function]] = []
    for item in functions:
        if item.kind != "c" or names.intersection((item.name, *item.aliases)):
            continue
        source = project.src / (item.path + ".c")
        if not source.is_file():
            continue
        try:
            c = source.read_text()
        except (OSError, UnicodeError) as error:
            raise Held("similar", f"{source}: {error}") from error
        if (
            not c.strip()
            or is_partial(c)
            or re.search(r"^\s*#\s*(?:ifn?def\s+NON_MATCHING|if\b[^\n]*NON_MATCHING)", c, re.M)
        ):
            continue
        candidate = opcodes(split.words(project, item))
        # Once top-k is full, candidates above its worst normalized distance
        # cannot enter it. Tighten the band without changing the ranking.
        candidate_bound = bound
        if len(ranked) == top_k:
            candidate_bound = min(bound, int(ranked[-1][0] * max(len(tokens), len(candidate)) + 1e-9))
        edits = levenshtein(tokens, candidate, candidate_bound)
        if edits > candidate_bound:
            continue
        distance = edits / max(len(tokens), len(candidate))
        ranked.append((distance, -overlap(tokens, candidate), item.name, edits, source, c, item))
        ranked.sort(key=lambda row: row[:3])
        del ranked[top_k:]
    results = []
    for distance, negative_overlap, name, edits, source, c, item in ranked[:top_k]:
        paths = sorted((extracted / "asm").rglob(Path(item.path).name + ".s"))
        if len(paths) == 1:
            asm = paths[0].read_text()
        else:
            # Matched C normally has no extracted .s. Its ROM interval is the
            # original assembly, expressed without requiring a disassembler.
            words = struct.iter_unpack(">I", split.words(project, item))
            asm = f"glabel {name}\n" + "".join(
                f"/* {item.address + index * 4:08X} */ .word 0x{word:08X}\n" for index, (word,) in enumerate(words)
            )
        results.append(Similar(name, version, edits, distance, -negative_overlap, source, c, asm))
    return results


def context(examples: list[Similar]) -> str:
    """Keep complete landed C and assembly as non-declaring m2c context."""
    parts = []
    for example in examples:
        text = (
            f"Similar matched function {example.function} VERSION {example.version}; "
            f"edit_distance={example.edit_distance}; distance={example.distance:.6f}; overlap={example.overlap:.6f}\n"
            f"C ({example.source}):\n{example.c}\nAssembly:\n{example.asm}"
        )
        # Line comments cannot be terminated by comments inside landed C.
        parts.append("\n".join("// " + line for line in text.splitlines()))
    return "\n\n".join(parts) + ("\n" if parts else "")
