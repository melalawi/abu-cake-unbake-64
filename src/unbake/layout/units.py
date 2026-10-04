"""Unit view of split rows: one pool row per maximal run with a single private owner."""

from __future__ import annotations

import re
from pathlib import Path

from unbake.layout import split

_RESERVED = ("shared", "unresolved", "writable")


def _owner(match: re.Match[str]) -> tuple[str, str] | None:
    """Kind and private owner of a structural pool row, else None."""
    parts = Path(match["path"].strip().strip("'\"")).parts
    if len(parts) != 3 or parts[0] != "rodata" or parts[1] in _RESERVED:
        return None
    return match["kind"], parts[1]


def merge_pools(yaml: str) -> str:
    """Keep the first row of each adjacent same-kind same-owner run and drop the rest.

    Rows with an explicit alignment stay run starts, and shared, unresolved or
    writable pools keep their per-symbol rows because ownership is ambiguous.
    """
    output = []
    previous: tuple[str, str] | None = None
    for line in yaml.splitlines(keepends=True):
        match = split.ROW.fullmatch(line)
        key = _owner(match) if match else None
        if key is not None and key == previous and match and not match["alignment"]:
            continue
        previous = key
        output.append(line)
    return "".join(output)
