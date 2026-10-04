"""The report units of one version: every text row, in split order."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from unbake.config import Version
from unbake.layout import split


@dataclass(frozen=True)
class Function:
    name: str
    path: str
    kind: str
    start: int
    end: int
    address: int | None


def functions(version: Version) -> list[Function]:
    """Count every text row, including startup and handwritten assembly."""
    _, _, segments = split.layout(version.split)
    return [
        Function(
            Path(row.path).name,
            row.path,
            row.kind,
            row.start,
            segment.rows[index + 1].start
            if index + 1 < len(segment.rows)
            else split.number(segment.end, "segment end"),
            split.address(row, version.split) if row.kind == "c" else None,
        )
        for segment in segments
        for index, row in enumerate(segment.rows)
        if row.kind in ("asm", "hasm", "c")
    ]
