"""Lower externs that name a function's private compiler pool back to literals.

A draft may read a float constant through the address splat gave its pool word.
The pool belongs to the compiler, so the source must spell the literal and let
the compiler emit the word that layout then proves against the ROM.
"""

from __future__ import annotations

import math
import re
import struct

from unbake.layout import split
from unbake.match import reporting
from unbake.project.config import Project

_TYPES = {"f32": (">f", "f"), "float": (">f", "f"), "f64": (">d", ""), "double": (">d", "")}
_EXTERN = re.compile(r"^[ \t]*extern\s+(?:const\s+)?(f32|f64|float|double)\s+(D_([0-9A-Fa-f]{8}))\s*;[ \t]*\n?", re.M)


def _slices(project: Project, function: str, version: str) -> dict[int, bytes]:
    """Private pool slices owned by one function, keyed by runtime address."""
    _, _, segments = split.layout(project.version(version).split)
    image = project.version(version).baserom.read_bytes()
    result = {}
    for segment in segments:
        rows = segment.rows
        for index, row in enumerate(rows):
            parts = row.path.split("/")
            if row.kind != "rodata" or len(parts) != 3 or parts[:2] != ["rodata", function]:
                continue
            end = rows[index + 1].start if index + 1 < len(rows) else segment.end
            if end is None or end <= row.start:
                continue
            result[int(parts[2], 16)] = image[row.start : end]
    return result


def _read(slices: dict[int, bytes], address: int, size: int) -> bytes | None:
    for base, content in slices.items():
        if base <= address and address + size <= base + len(content):
            return content[address - base : address - base + size]
    return None


def lower(project: Project, function: str, text: str, versions: tuple[str, ...]) -> str:
    """Replace each provable pool extern with its exact literal; leave the rest unchanged."""
    externs = list(_EXTERN.finditer(text))
    if not externs:
        return text
    pools = {version: _slices(project, function, version) for version in versions}
    for match in reversed(externs):
        kind, name, address = match[1], match[2], int(match[3], 16)
        layout, suffix = _TYPES[kind]
        size = struct.calcsize(layout)
        named = [content for pool in pools.values() if (content := _read(pool, address, size)) is not None]
        if not named:
            continue
        value = named[0]
        # Every version must hold the same word in this function's own pool.
        if any(not any(value in content for content in pool.values()) for pool in pools.values()):
            continue
        number = struct.unpack(layout, value)[0]
        rest = text[: match.start()] + text[match.end() :]
        if not math.isfinite(number) or re.search(rf"&\s*{name}\b", rest):
            continue
        literal = f"({number!r}{suffix})"
        text = re.sub(rf"\b{name}\b", literal, rest)
        reporting.learn(f"OK(types): {function}: lower pool extern {name} -> {literal}")
    return text
