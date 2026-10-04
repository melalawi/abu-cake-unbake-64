"""The `resident` step: resident constant blocks carry no addresses and versions share identical blocks.

A published source keeps the bytes of resident constants it owns in a marked block of `const` definitions,
one `#if defined(MACRO) ...` branch per group of versions. The link script discards these sections, and each
version's addresses live in its split rows, so a name holds only the unit and an ordinal:
`unbake_rodata_<UNIT>_<N>`. Versions whose definitions are equal (types, initializers, order) share one branch;
when every version agrees the block is unconditional. The same text then preprocesses identically in those
versions and compiles once. The ordinal base of each block is the previous block's base plus its longest
branch, so a shared definition gets the same name in every version. Rewritten sources are committed.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import Path

from unbake import atomic as atomic_files
from unbake import cache, steps
from unbake.config import Held, Host, Project

MARKER = "/* Native resident constant storage; absolute access symbols retain their addresses. */"
_DEFINITION = re.compile(r"const ([A-Za-z_][\w ]*?) unbake_rodata_\w+?((?:\[\w*\])*) = (.+);")
_BRANCH = re.compile(r"#(if|elif) (defined\(\w+\)(?: \|\| defined\(\w+\))*)")

Definition = tuple[str, str, str]


def input_key(project: Project) -> str:
    return cache.key(steps.tool_fingerprint(), *sorted(project.src.glob("*.c")))


def canonical(text: str, unit: str, macros: Mapping[str, tuple[str, ...]]) -> str:
    """TEXT with every marked block renamed and grouped; MACROS maps each version, in order, to its macros."""
    owner = {macro: version for version, names in macros.items() for macro in names}
    lines = text.split("\n")
    out: list[str] = []
    base = at = 0
    while at < len(lines):
        out.append(lines[at])
        if lines[at] != MARKER:
            at += 1
            continue
        blocks, at = _parse(lines, at + 1, unit, owner, tuple(macros))
        out.extend(_render(blocks, base, unit, macros))
        base += max((len(rows) for rows in blocks.values()), default=0)
        if not any(blocks.values()):
            out.pop()
    return "\n".join(out)


def _parse(
    lines: list[str], at: int, unit: str, owner: Mapping[str, str], versions: tuple[str, ...]
) -> tuple[dict[str, list[Definition]], int]:
    """Each version's definitions in the block starting at AT, and the index after the block."""
    found: dict[str, list[Definition]] = {version: [] for version in versions}
    if at < len(lines) and not lines[at].startswith("#"):
        rows, at = _definitions(lines, at)
        return {version: list(rows) for version in versions}, at
    seen: set[str] = set()
    while at < len(lines):
        line = lines[at]
        if line == "#endif":
            return found, at + 1
        branch = _BRANCH.fullmatch(line)
        if branch is None or (branch[1] == "if") != (not seen):
            raise Held("resident", f"resident.{unit}: line {at + 1}: unexpected {line!r} in a resident block")
        members = []
        for macro in re.findall(r"defined\((\w+)\)", branch[2]):
            if macro not in owner:
                raise Held("resident", f"resident.{unit}: {macro} is no version's macro")
            if owner[macro] in seen:
                raise Held("resident", f"resident.{unit}: version {owner[macro]} has two branches")
            seen.add(owner[macro])
            members.append(owner[macro])
        rows, at = _definitions(lines, at + 1)
        for version in members:
            found[version] = list(rows)
    raise Held("resident", f"resident.{unit}: resident block has no #endif")


def _definitions(lines: list[str], at: int) -> tuple[list[Definition], int]:
    rows = []
    while at < len(lines) and (match := _DEFINITION.fullmatch(lines[at])) is not None:
        rows.append((match[1], match[2], match[3]))
        at += 1
    return rows, at


def _render(
    blocks: Mapping[str, list[Definition]], base: int, unit: str, macros: Mapping[str, tuple[str, ...]]
) -> list[str]:
    groups: dict[tuple[Definition, ...], list[str]] = {}
    for version in macros:
        groups.setdefault(tuple(blocks[version]), []).append(version)

    def body(rows: tuple[Definition, ...]) -> list[str]:
        return [
            f"const {kind} unbake_rodata_{unit}_{base + n}{extent} = {value};"
            for n, (kind, extent, value) in enumerate(rows)
        ]

    if len(groups) == 1:
        return body(next(iter(groups)))
    out: list[str] = []
    for rows, versions in groups.items():
        if rows:
            test = " || ".join(f"defined({macros[version][0]})" for version in versions)
            out += [f"#{'elif' if out else 'if'} {test}", *body(rows)]
    return [*out, "#endif"]


def run(project: Project, host: Host) -> list[Path]:
    """Rewrite every published source whose resident blocks are not canonical; commit them together."""
    from unbake import land

    macros = {version: project.version(version).macros for version in project.versions}
    changed = []
    for path in sorted(project.src.glob("*.c")):
        text = path.read_text()
        if MARKER not in text:
            continue
        result = canonical(text, path.stem, macros)
        if result != text:
            atomic_files.text(path, result)
            changed.append(path)
    if changed:
        land._commit(project, host, changed, "Name resident constants without addresses")
    return changed
