"""Split-row edits the shape rules prove from ROM words alone; the extract step applies them before splat.

- Filler: an asm row whose first words are alignment filler (work.shape.filler) starts at its aligned entry;
  the filler becomes a data row `PATH_padding_START`. The row's symbols move to the entry. A `func_HEX...`
  name is renamed to the entry address (HEX + filler bytes) when every version holding it has the same filler,
  so the name keeps telling where the function starts in every version.
- Tail: an asm row that continues the asm row before it (work.shape.tail) is folded into that row; its row
  and its symbols are deleted.
Renames and folded tails are carried into layout.toml members and unbake-exclusions.json.
Refused (left as is, reported): a symbol already at the entry, rows in different segments, or a name that a
published src/*.c uses. The build copies asm and data rows from the ROM, so bytes cannot change.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from unbake import atomic as atomic_files
from unbake.config import Held
from unbake.decomp.exclusions import MANIFEST
from unbake.layout import split
from unbake.work import shape

if TYPE_CHECKING:
    from unbake.config import Host, Project

_FUNC = re.compile(r"func_([0-9A-F]{8})(\w*)")
_IDENTIFIER = re.compile(r"\b[A-Za-z_]\w*\b")


@dataclass(frozen=True)
class Finding:
    """One proved edit in one VERSION: kind `filler` (skip bytes before the entry) or `tail` (of owner)."""

    version: str
    kind: str
    name: str
    start: int
    address: int
    skip: int = 0
    owner: str = ""


@dataclass(frozen=True)
class Scan:
    version: str
    findings: tuple[Finding, ...]
    names: frozenset[str]
    blocked: tuple[str, ...] = ()


def scan(job: tuple[Project, str]) -> Scan:
    """Pool worker: the filler and tail findings of one VERSION's asm rows, and every row and symbol name."""
    project, version = job
    config = project.version(version)
    rows = split.functions(project, version)
    image = config.baserom.read_bytes()
    _, symbols = split.symbols(config.symbols)
    segment_of = {row.start: id(segment) for segment in split.layout(config.split)[2] for row in segment.rows}
    targets = {ident: shape.for_compiler(compiler) for ident, compiler in project.compilers.items()}
    taken = {entry[0] for entry in symbols.values()}
    found, blocked = [], []
    for index, row in enumerate(rows):
        if row.kind != "asm":
            continue
        target = targets[project.compiler_for(Path(row.path).name).id]
        data = image[row.start : row.end]
        name = Path(row.path).name
        previous = rows[index - 1] if index else None
        if (
            previous is not None
            and previous.kind == "asm"
            and previous.end == row.start
            and segment_of[previous.start] == segment_of[row.start]
            and shape.tail(image[previous.start : previous.end], previous.address, data, target)
        ):
            found.append(Finding(version, "tail", name, row.start, row.address, owner=Path(previous.path).name))
            continue
        skip = 4 * shape.filler(shape.words_of(data), row.address, target)
        if skip and row.address + skip in taken:
            blocked.append(name)
        elif skip:
            found.append(Finding(version, "filler", name, row.start, row.address, skip))
    names = frozenset({Path(row.path).name for row in rows} | set(symbols))
    return Scan(version, tuple(found), names, tuple(blocked))


def published_names(project: Project) -> set[str]:
    """Every identifier a published C source uses."""
    return {word for path in sorted(project.src.glob("*.c")) for word in _IDENTIFIER.findall(path.read_text())}


def renames(scans: Iterable[Scan], used: set[str]) -> dict[str, str]:
    """func_HEX names to rename to their entry: every holding VERSION has the same filler, no published C uses
    the name, and the new name is free in every VERSION."""
    scans = list(scans)
    skips: dict[str, set[int]] = {}
    for item in scans:
        for finding in item.findings:
            if finding.kind == "filler":
                skips.setdefault(finding.name, set()).add(finding.skip)
    taken = set().union(*(item.names for item in scans)) if scans else set()
    result = {}
    for name, sizes in skips.items():
        match = _FUNC.fullmatch(name)
        holding = [item for item in scans if name in item.names]
        filled = [item for item in holding if any(f.name == name and f.kind == "filler" for f in item.findings)]
        if match is None or len(sizes) != 1 or len(filled) != len(holding) or name in used:
            continue
        new = f"func_{int(match[1], 16) + next(iter(sizes)):08X}{match[2]}"
        if new not in taken and new not in used:
            result[name] = new
    return result


def split_text(path: Path, text: str, findings: Iterable[Finding], renamed: dict[str, str]) -> str:
    """The split with each filler row cut at its entry and each tail row deleted."""
    _, lines, segments = split.parse_layout(path, text)
    rows = {row.start: row for segment in segments for row in segment.rows}
    for finding in findings:
        row = rows.get(finding.start)
        if row is None or row.kind != "asm" or Path(row.path).name != finding.name:
            raise Held("shape-edits", f"{path}: {finding.name}: no asm row at 0x{finding.start:X}")
        if finding.kind == "tail":
            index = row.segment.rows.index(row)
            owner = row.segment.rows[index - 1] if index else None
            if owner is None or Path(owner.path).name != finding.owner:
                raise Held("shape-edits", f"{path}: {finding.name}: owner {finding.owner} not before it")
            lines[row.line] = ""
            continue
        template = lines[row.line]
        newline = row.match["newline"] or "\n"
        padding = split.replace_row(template, row.match, kind="data", path=f"{row.path}_padding_{finding.start:X}")
        path_text = str(Path(row.path).with_name(renamed.get(finding.name, finding.name)))
        entry = split.replace_row(template, row.match, start=f"0x{finding.start + finding.skip:06X}", path=path_text)
        lines[row.line] = padding.rstrip("\r\n") + newline + entry
    return "".join(lines)


def symbols_text(path: Path, text: str, findings: Iterable[Finding], renamed: dict[str, str]) -> str:
    """The symbol file with each filler row's symbols moved to its entry and each tail row's symbols deleted."""
    moves = {f.address: f for f in findings if f.kind == "filler"}
    drops = {f.address for f in findings if f.kind == "tail"}
    output = []
    for line in text.splitlines(keepends=True):
        match = split.SYMBOL.match(line)
        address = int(match["address"], 0) if match else None
        if address in drops:
            continue
        if match and address in moves:
            finding = moves[address]
            name = renamed.get(match["name"], match["name"]) if match["name"] == finding.name else match["name"]
            line = (
                line[: match.start("name")]
                + name
                + line[match.end("name") : match.start("address")]
                + f"0x{address + finding.skip:08X}"
                + line[match.end("address") :]
            )
        output.append(line)
    return "".join(output)


def run(project: Project, host: Host) -> list[str]:
    """Scan every VERSION in the worker pool, then write all splits, symbols, name lists and build files, and
    commit; any failure restores every file it wrote."""
    from unbake import buildfiles, pool
    from unbake import config as project_config

    scans = pool.run(host, scan, [(project, version) for version in project.versions])
    used = published_names(project)
    kept = {item.version: [f for f in item.findings if f.name not in used] for item in scans}
    lines = [
        f"shape edit refused: {f.version} {f.name}: published C uses it"
        for i in scans
        for f in i.findings
        if f.name in used
    ]
    lines += [
        f"shape edit refused: {i.version} {name}: a symbol already names its entry" for i in scans for name in i.blocked
    ]
    if not any(kept.values()):
        return lines
    renamed = renames((Scan(item.version, tuple(kept[item.version]), item.names) for item in scans), used)
    layout = project.root / "layout.toml"
    paths = [
        layout,
        project.root / MANIFEST,
        *(p for v in project.versions for p in (project.version(v).split, project.version(v).symbols)),
    ]
    backup = {path: path.read_bytes() for path in paths if path.is_file()}
    try:
        for version, findings in kept.items():
            if not findings:
                continue
            config = project.version(version)
            for path, edit in ((config.split, split_text), (config.symbols, symbols_text)):
                atomic_files.text(path, edit(path, split.read(path), findings, renamed))
        _relabel(project, renamed, {f.name for found in kept.values() for f in found if f.kind == "tail"})
        # The generated build files name symbols and rows too; they land in the same commit.
        generated = buildfiles.write(project_config.load(project.root), host)
        _commit(project, host, sorted({*backup, *generated}), kept, renamed)
    except BaseException:
        for path, content in backup.items():
            atomic_files.write(path, content)
        buildfiles.write(project_config.load(project.root), host)
        raise
    for findings in kept.values():
        for f in findings:
            target = f"into {f.owner}" if f.kind == "tail" else f"+0x{f.skip:X} as {renamed.get(f.name, f.name)}"
            lines.append(f"shape edit {f.version} {f.kind} {f.name} {target}")
    return lines


def _relabel(project: Project, renamed: dict[str, str], tails: set[str]) -> None:
    """Carry renames into the files that list function names: layout.toml members and the exclusions manifest.
    A folded tail no VERSION holds any more leaves both; one still held elsewhere gets its version marks again."""
    import json
    import tomllib

    from unbake.layout import map as layout_map

    remaining = layout_map.catalog(project)
    gone = {name for name in tails if name not in remaining}
    manifest = project.root / MANIFEST
    if manifest.is_file():
        value = json.loads(manifest.read_text())
        names = [renamed.get(name, name) for name in value["functions"] if name not in gone]
        if names != value["functions"]:
            atomic_files.text(manifest, json.dumps({**value, "functions": names}, indent=2) + "\n")
    path = project.root / "layout.toml"
    if not path.is_file():
        return
    present = {name for group in tomllib.loads(path.read_text()).get("group", []) for name in group["members"]}
    replacements: dict[str, tuple[str, ...]] = {old: (new,) for old, new in renamed.items() if old in present}
    replacements.update({name: () if name in gone else (name,) for name in tails if name in present})
    # Always rewritten: a moved entry can change the address order of members that keep their names.
    layout_map.edit_members(project, replacements)


def _commit(
    project: Project, host: Host, paths: list[Path], kept: dict[str, list[Finding]], renamed: dict[str, str]
) -> None:
    from unbake.layout import merge_units

    found = [f for findings in kept.values() for f in findings]
    fillers, tails = sum(f.kind == "filler" for f in found), sum(f.kind == "tail" for f in found)
    message = f"Shape edits: {fillers} rows start past alignment filler ({len(renamed)} renamed), {tails} tails folded"
    merge_units._commit(project, host, paths, message)
