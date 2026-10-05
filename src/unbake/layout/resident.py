"""The `resident` step: delete resident constant blocks from published sources.

A marked block of `unbake_rodata_*` const definitions (optionally in per-version `#if` branches) restates
resident bytes in C. The generated link script keeps only .text and the slices supply the ROM's rodata, so
these bytes never reach a ROM. The step removes each block, refuses one whose names the rest of the source
still spells, and commits the rewritten sources together. The `resident-storage` source rule refuses any new one.
"""

from __future__ import annotations

import re
from pathlib import Path

from unbake import atomic as atomic_files
from unbake import cache
from unbake.config import Held, Host, Project

MARKER = "/* Native resident constant storage; absolute access symbols retain their addresses. */"
_DEFINITION = re.compile(r"const [A-Za-z_][\w ]*? (unbake_rodata_\w+?)(?:\[\w*\])* = .+;")
_BRANCH = re.compile(r"#(?:elif|else)\b.*")

# Bump when this step's output changes for the same inputs. Keys never digest the tool's code.
SCHEMA = 1


def input_key(project: Project) -> str:
    return cache.key("resident", str(SCHEMA), *sorted(project.src.glob("*.c")))


def deleted(text: str, unit: str) -> str:
    """TEXT without its marked resident blocks (and the blank line before each)."""
    lines = text.split("\n")
    out: list[str] = []
    names: set[str] = set()
    at = 0
    while at < len(lines):
        if lines[at] != MARKER:
            out.append(lines[at])
            at += 1
            continue
        if out and not out[-1]:
            out.pop()
        at += 1
        guarded = at < len(lines) and lines[at].startswith("#if")
        at += guarded
        while at < len(lines):
            match = _DEFINITION.fullmatch(lines[at])
            if match is None and not (guarded and _BRANCH.fullmatch(lines[at])):
                break
            if match is not None:
                names.add(match[1])
            at += 1
        if guarded:
            if at >= len(lines) or lines[at] != "#endif":
                found = repr(lines[at]) if at < len(lines) else "the end of the file"
                raise Held("resident", f"resident.{unit}: line {at + 1}: expected #endif, found {found}")
            at += 1
    result = "\n".join(out)
    used = sorted(name for name in names if re.search(rf"\b{name}\b", result))
    if used:
        raise Held("resident", f"resident.{unit}: source still uses {', '.join(used)}")
    return result


def run(project: Project, host: Host) -> list[Path]:
    """Delete every published source's resident blocks; commit the changed sources together."""
    from unbake import land

    changed = []
    for path in sorted(project.src.glob("*.c")):
        text = path.read_text()
        if MARKER in text:
            atomic_files.text(path, deleted(text, path.stem))
            changed.append(path)
    if changed:
        land._commit(project, host, changed, "Delete resident constant blocks the link discards")
    return changed
