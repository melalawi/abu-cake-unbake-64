"""Split-row edits the shape rules prove from ROM words alone; the extract step applies them before splat.

- Filler: an asm row whose first words are alignment filler (work.shape.filler) starts at its aligned entry;
  the filler becomes a data row `PATH_padding_START`. The row's symbols move to the entry. A `func_HEX...`
  name is renamed to the entry address (HEX + filler bytes) when every version holding it has the same filler,
  so the name keeps telling where the function starts in every version.
- Continuation: adjacent compatible asm rows are coalesced only when the first is incomplete,
  the next requires entry state it does not establish, and the combined body passes the shared boundary
  and compiler proofs. Independent references and published aliases protect an entry.
Renames and removed continuation rows are carried into layout.toml and unbake-exclusions.json; a renamed function's
draft history (build/work/FUNC/) and its attempts.json record move to the new name in the same publish.
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
from unbake.layout import boundary, split
from unbake.process import named as cause_named
from unbake.work import attempts, shape

if TYPE_CHECKING:
    from unbake.compilers.families.mips import Shape
    from unbake.config import Host, Project

_FUNC = re.compile(r"func_([0-9A-F]{8})(\w*)")
_IDENTIFIER = re.compile(r"\b[A-Za-z_]\w*\b")


@dataclass(frozen=True)
class Finding:
    """One proved filler or continuation edit; OWNER and aliases pin any removed entry."""

    version: str
    kind: str
    name: str
    start: int
    address: int
    skip: int = 0
    owner: str = ""
    aliases: tuple[str, ...] = ()


@dataclass(frozen=True)
class Scan:
    version: str
    findings: tuple[Finding, ...]
    names: frozenset[str]
    blocked: tuple[str, ...] = ()


def scan(job: tuple[Project, str]) -> Scan:
    """Pool worker: proved filler/continuation findings and every row/symbol name."""
    project, version = job
    config = project.version(version)
    rows = split.functions(project, version)
    image = config.baserom.read_bytes()
    _, symbols = split.symbols(config.symbols)
    taken = {entry[0] for entry in symbols.values()}
    found, blocked = [], []
    segment_of = {r.start: id(segment) for segment in split.layout(config.split)[2] for r in segment.rows}
    targets, emitted = shape.configured(project)
    consumed: set[int] = set()

    def continuation(index: int, target: Shape) -> list[Finding]:
        if index == 0 or "tail" not in target.rules:
            return []
        first = shape.words_of(image[rows[index].start : rows[index].end])
        if any(shape._frame_open(word) for word in first) or not shape._reads_unset(first, target):
            return []
        previous = rows[index - 1]
        if previous.kind != "asm" or previous.start in consumed:
            return []
        owner = Path(previous.path).name
        compiler = project.compiler_for(owner)
        flags = project.recipe_for(owner).phase("compile")
        bias = previous.address - previous.start
        known = {f.address - bias for f in rows}
        code = {at: int.from_bytes(image[at : at + 4], "big") for at in range(previous.start, previous.end, 4)}
        if boundary.evidence(code, previous.start, previous.end, bias, {"split-entry"}, known, target).proven:
            return []
        group = []
        end = previous.end
        for following in rows[index:]:
            name = Path(following.path).name
            words = first if following is rows[index] else shape.words_of(image[following.start : following.end])
            if (
                following.kind != "asm"
                or following.start != end
                or segment_of[previous.start] != segment_of[following.start]
                or following.address - following.start != bias
                or project.compiler_for(name) != compiler
                or project.recipe_for(name).phase("compile") != flags
                or any(shape._frame_open(word) for word in words)
                or not shape._reads_unset(words, target)
            ):
                break
            group.append(following)
            code.update(
                {at: int.from_bytes(image[at : at + 4], "big") for at in range(following.start, following.end, 4)}
            )
            known.discard(following.start)
            end = following.end
            proof = boundary.evidence(code, previous.start, end, bias, {"split-entry"}, known, target)
            if proof.proven:
                if (
                    shape.classify(image[previous.start : end], previous.address, target, emitted, proof)[0]
                    != "drafter"
                ):
                    break
                return [
                    Finding(
                        version,
                        "continuation",
                        Path(f.path).name,
                        f.start,
                        f.address,
                        owner=owner,
                        aliases=tuple(n for n, entry in symbols.items() if entry[0] == f.address),
                    )
                    for f in group
                ]
        return []

    for index, row in enumerate(rows):
        if row.kind != "asm" or row.start in consumed:
            continue
        compiler = project.compiler_for(Path(row.path).name)
        target = targets.get(Path(row.path).name, targets[compiler.id])
        data = image[row.start : row.end]
        name = Path(row.path).name
        continued = continuation(index, target)
        if continued:
            found.extend(continued)
            consumed.update(f.start for f in continued)
            continue
        skip = 4 * shape.filler(shape.words_of(data), row.address, target)
        if skip and row.address + skip in taken:
            blocked.append(name)
        elif skip:
            found.append(Finding(version, "filler", name, row.start, row.address, skip))
    continuations = {f.address: f for f in found if f.kind == "continuation"}
    if continuations:
        from unbake.layout.rodata_references import collect

        protected: set[int] = set()
        owners = {f.name: f.owner for f in continuations.values()}
        for function in rows:
            body = image[function.start : function.end]
            refs, _ = collect(function.name, body, None, None)
            protected.update(r.address for r in refs if r.address in continuations)
            for offset in range(function.start, function.end, 4):
                word = int.from_bytes(image[offset : offset + 4], "big")
                pc = function.address + offset - function.start
                op = word >> 26
                target_address = None
                if op in (2, 3):
                    target_address = ((pc + 4) & 0xF0000000) | ((word & 0x3FFFFFF) << 2)
                elif op in (1, 4, 5, 6, 7, 20, 21, 22, 23) or (op == 17 and word >> 21 & 31 == 8):
                    target_address = pc + 4 + ((word & 0x7FFF) - (word & 0x8000)) * 4
                finding = continuations.get(target_address) if target_address is not None else None
                if finding is not None and owners.get(function.name, function.name) != finding.owner:
                    protected.add(finding.address)
        # A stored pointer is a reason to retain an independently addressable entry.
        for segment in split.layout(config.split)[2]:
            for data_row in segment.rows:
                if data_row.kind in ("data", "rodata", "rdata"):
                    for offset in range((data_row.start + 3) // 4 * 4, min(split.end(data_row), len(image)) - 3, 4):
                        pointer = int.from_bytes(image[offset : offset + 4], "big")
                        protected.update(a for a in (pointer, pointer | 0x80000000) if a in continuations)
        protected_owners = {f.owner for f in continuations.values() if f.address in protected}
        found = [f for f in found if f.kind != "continuation" or f.owner not in protected_owners]
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
    """Cut filler at its entry and coalesce proved continuation rows."""
    _, lines, segments = split.parse_layout(path, text)
    rows = {row.start: row for segment in segments for row in segment.rows}
    findings = tuple(findings)
    owners = {f.name: f.owner for f in findings if f.kind == "continuation"}
    for finding in findings:
        row = rows.get(finding.start)
        if row is None or row.kind != "asm" or Path(row.path).name != finding.name:
            raise Held(
                cause_named(
                    f"{path}",
                    f"{path}: {finding.name}: no asm row at 0x{finding.start:X}",
                    owner="layout.shape_edits",
                    stage="shape-edits",
                )
            )
        if finding.kind == "continuation":
            position = row.segment.rows.index(row)
            previous = row.segment.rows[position - 1] if position else None
            previous_name = Path(previous.path).name if previous is not None else ""
            if previous is None or owners.get(previous_name, previous_name) != finding.owner:
                raise Held(
                    cause_named(
                        f"{path}",
                        f"{path}: {finding.name}: owner {finding.owner} not before it",
                        owner="layout.shape_edits",
                        stage="shape-edits",
                    )
                )
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
    """Move filler symbols and remove only proved, unused continuation symbols."""
    moves = {f.address: f for f in findings if f.kind == "filler"}
    removed = {f.address for f in findings if f.kind == "continuation"}
    output = []
    for line in text.splitlines(keepends=True):
        match = split.SYMBOL.match(line)
        address = int(match["address"], 0) if match else None
        if address in removed:
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
    kept = {}
    for item in scans:
        protected_owners = {f.owner for f in item.findings if f.owner and {f.name, f.owner, *f.aliases} & used}
        kept[item.version] = [
            f for f in item.findings if not {f.name, f.owner, *f.aliases} & used and f.owner not in protected_owners
        ]
    lines = [
        f"shape edit refused: {f.version} {f.name}: published C uses it"
        for i in scans
        for f in i.findings
        if {f.name, f.owner, *f.aliases} & used
    ]
    lines += [
        f"shape edit refused: {i.version} {name}: a symbol already names its entry" for i in scans for name in i.blocked
    ]
    if not any(kept.values()):
        return lines
    renamed = renames((Scan(item.version, tuple(kept[item.version]), item.names) for item in scans), used)
    layout = project.root / "layout.toml"
    paths = [
        project.root / "config.toml",
        layout,
        project.root / MANIFEST,
        project.root / attempts.PATH,
        *(p for v in project.versions for p in (project.version(v).split, project.version(v).symbols)),
    ]
    # Draft history and attempt records move with a rename in the same publish: staged first, swapped in after
    # the commit, discarded on any failure.
    carries = attempts.stage_renames(project, renamed)
    from unbake import journal

    try:
        with journal.transaction(project):
            attempts.ledger(project).rename(renamed)
            for version, findings in kept.items():
                if not findings:
                    continue
                config = project.version(version)
                for path, edit in ((config.split, split_text), (config.symbols, symbols_text)):
                    atomic_files.text(path, edit(path, split.read(path), findings, renamed))
            _relabel(
                project, renamed, {f.name for findings in kept.values() for f in findings if f.kind == "continuation"}
            )
            # The generated build files name symbols and rows too; they land in the same commit.
            generated = buildfiles.write(project_config.load(project.root), host)
            _commit(project, host, sorted({*paths, *generated}), kept, renamed)
    except BaseException:
        attempts.discard(carries)
        raise
    attempts.install(carries)
    for findings in kept.values():
        for f in findings:
            target = (
                f"into {f.owner}" if f.kind == "continuation" else f"+0x{f.skip:X} as {renamed.get(f.name, f.name)}"
            )
            lines.append(f"shape edit {f.version} {f.kind} {f.name} {target}")
    return lines


def _relabel(project: Project, renamed: dict[str, str], continuations: set[str]) -> None:
    """Carry renames into the files that list function names: layout.toml members and the exclusions manifest."""
    import json
    import tomllib

    import toml  # type: ignore[import-untyped]

    from unbake.layout import map as layout_map

    remaining = layout_map.catalog(project)
    gone = continuations - remaining.keys()
    configuration = project.root / "config.toml"
    configured = tomllib.loads(configuration.read_text())
    units = configured.get("units", {})
    for old, new in renamed.items():
        if old in units and new in units:
            raise Held(
                cause_named(
                    "shape.config",
                    f"shape.config: {old}: renamed unit {new} already configured",
                    owner="layout.shape_edits",
                    stage="shape-edits",
                )
            )
    relabeled = {renamed.get(name, name): row for name, row in units.items() if name not in gone}
    if relabeled != units:
        configured["units"] = relabeled
        atomic_files.text(configuration, toml.dumps(configured))
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
    replacements.update({name: () if name in gone else (name,) for name in continuations if name in present})
    # Always rewritten: a moved entry can change the address order of members that keep their names.
    layout_map.edit_members(project, replacements)


def _commit(
    project: Project, host: Host, paths: list[Path], kept: dict[str, list[Finding]], renamed: dict[str, str]
) -> None:
    from unbake import land

    found = [f for findings in kept.values() for f in findings]
    message = (
        f"Shape edits: {sum(f.kind == 'filler' for f in found)} filler rows ({len(renamed)} renamed), "
        f"{sum(f.kind == 'continuation' for f in found)} proved continuations"
    )
    land._commit(project, host, paths, message)
