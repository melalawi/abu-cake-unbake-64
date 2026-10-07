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
from unbake import cache, inputs
from unbake.config import Held, Host, Project
from unbake.process import named as cause_named

MARKER = "/* Native resident constant storage; absolute access symbols retain their addresses. */"
_DEFINITION = re.compile(r"const [A-Za-z_][\w ]*? (unbake_rodata_\w+?)(?:\[\w*\])* = .+;")
_BRANCH = re.compile(r"#(?:elif|else)\b.*")
_COMMENT = re.compile(r"/\*.*\*/|//.*")

# Bump when this step's output changes for the same inputs. Keys never digest the tool's code.
SCHEMA = 4


def input_key(project: Project) -> str:
    dependencies = inputs.DependencySet(
        tuple(
            inputs.file_pin(path, root=project.root, root_id="project", reuse=cache.configured())
            for path in sorted(project.src.glob("*.c"))
        ),
        {},
        {"resident-inputs": inputs.digest(Path(__file__), algorithm="sha256", reuse=cache.configured())},
    )
    return cache.key("resident", str(SCHEMA), dependencies.digest)


def deleted(text: str, unit: str) -> str:
    """TEXT without its marked resident blocks (and the blank line before each)."""
    lines = text.split("\n")
    out: list[str] = []
    names: set[str] = set()
    at = 0
    while at < len(lines):
        if lines[at].strip() != MARKER:
            out.append(lines[at])
            at += 1
            continue
        if out and not out[-1]:
            out.pop()
        at += 1
        guarded = at < len(lines) and lines[at].startswith("#if")
        at += guarded
        while at < len(lines):
            line = lines[at].strip()
            if guarded and (not line or _COMMENT.fullmatch(line)):
                at += 1
                continue
            match = _DEFINITION.fullmatch(line)
            if match is None and not (guarded and _BRANCH.fullmatch(line)):
                break
            if match is not None:
                names.add(match[1])
            at += 1
        if guarded:
            if at >= len(lines) or lines[at].strip() != "#endif":
                found = repr(lines[at]) if at < len(lines) else "the end of the file"
                raise Held(
                    cause_named(
                        f"resident.{unit}",
                        f"resident.{unit}: line {at + 1}: expected #endif, found {found}",
                        owner="layout.resident",
                        stage="resident",
                    )
                )
            at += 1
    result = "\n".join(out)
    used = sorted(name for name in names if re.search(rf"\b{name}\b", result))
    if used:
        raise Held(
            cause_named(
                f"resident.{unit}",
                f"resident.{unit}: source still uses {', '.join(used)}",
                owner="layout.resident",
                stage="resident",
            )
        )
    return result


def run(project: Project, host: Host) -> list[Path]:
    """Delete every published source's resident blocks; commit the changed sources together."""
    from unbake import land

    changed = []
    refused = []
    for path in sorted(project.src.glob("*.c")):
        text = path.read_text()
        if MARKER in text:
            try:
                atomic_files.text(path, deleted(text, path.stem))
            except Held as error:
                refused.append(error.reason)
                continue
            changed.append(path)
    if changed:
        land._commit(project, host, changed, "Delete resident constant blocks the link discards")
    if refused:
        raise Held(cause_named("layout.resident.run", "; ".join(refused), owner="layout.resident", stage="resident"))
    return changed
