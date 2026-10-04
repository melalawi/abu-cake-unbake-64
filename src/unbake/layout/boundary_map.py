"""Bulk, byte-pinned split boundary edits with transactional ROM verification.

A JSON map is a list of records: version, function, action (merge, data, code or entry),
sha256, evidence, and neighbour for merges. Names identify existing split rows;
all versions are planned before any file is written. Merges keep the neighbour's
name, entry address and kind. Data rows retain their exact loaded ROM interval.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from unbake.config import Held
from unbake.layout import split, split_apply

if TYPE_CHECKING:
    from unbake.build import Outcome
    from unbake.config import Host, Project


@dataclass(frozen=True)
class Change:
    version: str
    function: str
    action: str
    sha256: str
    evidence: str
    neighbour: str = ""
    start: int | None = None


def read(path: Path) -> list[Change]:
    try:
        document = json.loads(path.read_text())
    except (OSError, ValueError) as error:
        raise Held("boundary-map", f"{path}: {error}") from error
    if not isinstance(document, list):
        raise Held("boundary-map", "map: required list of changes")
    result = []
    for index, row in enumerate(document):
        if not isinstance(row, dict) or set(row) - {
            "version",
            "function",
            "action",
            "sha256",
            "evidence",
            "neighbour",
            "start",
        }:
            raise Held("boundary-map", f"map row {index + 1}: invalid fields")
        if any(
            not isinstance(row.get(key), str) or not row[key]
            for key in ("version", "function", "action", "sha256", "evidence")
        ):
            raise Held("boundary-map", f"map row {index + 1}: missing string fields")
        if "neighbour" in row and not isinstance(row["neighbour"], str):
            raise Held("boundary-map", f"{row['function']}: invalid neighbour")
        if "start" in row and type(row["start"]) is not int:
            raise Held("boundary-map", f"{row['function']}: invalid data start")
        result.append(Change(**row))
    return result


def plan(project: Project, changes: Sequence[Change]) -> list[split.Edit]:
    """Validate every requested interval and coalesce each VERSION's split once."""
    selected: dict[str, list[Change]] = {}
    names: set[tuple[str, str]] = set()
    for change in changes:
        project.version(change.version)
        if change.action == "code" and not split.NAME.fullmatch(change.function):
            raise Held(
                "boundary-map",
                "split.data_to_code: numeric data stems need a named interval correction; "
                "use unbake split code FUNCTION --version V --start ROM --end ROM",
            )
        split.name(change.function)
        key = (change.version, change.function)
        if key in names:
            raise Held("boundary-map", f"{change.function}: duplicate change in VERSION {change.version}")
        names.add(key)
        if change.action not in ("merge", "data", "code", "entry") or not change.evidence.strip():
            raise Held("boundary-map", f"{change.function}: required merge/data/code/entry action and evidence")
        selected.setdefault(change.version, []).append(change)
    edits = []
    for version, items in selected.items():
        config = project.version(version)
        before, lines, segments = split.layout(config.split)
        image = config.baserom.read_bytes()
        symbols_before, symbols = split.symbols(config.symbols)
        symbols_after = symbols_before
        rows = [row for segment in segments for row in segment.rows]
        by_name: dict[str, list[split.Row]] = {}
        for row in rows:
            by_name.setdefault(Path(row.path).name, []).append(row)
        deleted: set[split.Row] = set()
        neighbours: set[split.Row] = set()
        owners_by_fragment: dict[split.Row, split.Row] = {}
        data: set[split.Row] = set()
        for change in items:
            found = by_name.get(change.function, [])
            kinds = ("data",) if change.action == "code" else ("asm", "hasm")
            if len(found) != 1 or found[0].kind not in kinds:
                raise Held(
                    "boundary-map", f"{change.function}: requires one {'/'.join(kinds)} row in VERSION {version}"
                )
            row = found[0]
            stop = split.end(row)
            if row.start < 0 or row.start >= stop or stop > len(image):
                raise Held("boundary-map", f"{change.function}: invalid ROM interval")
            digest = hashlib.sha256(image[row.start : stop]).hexdigest()
            if digest != change.sha256:
                raise Held("boundary-map", f"{change.function}: ROM bytes differ from map sha256")
            if change.action == "code":
                lines[row.line] = split.replace_row(lines[row.line], row.match, kind="asm")
                continue
            if change.action == "data":
                data.add(row)
                start = row.start if change.start is None else change.start
                if start < row.start or start >= stop or start % 4:
                    raise Held("boundary-map", f"{change.function}: data start outside word-aligned ROM interval")
                template = lines[row.line]
                if start == row.start:
                    lines[row.line] = split.replace_row(template, row.match, kind="data")
                else:
                    padding = row.path + f"_padding_{start:X}"
                    if any(item.path == padding for item in rows):
                        raise Held("boundary-map", f"{change.function}: padding path already exists")
                    newline = row.match["newline"] or "\n"
                    rendered = split.replace_row(template, row.match, start=f"0x{start:06X}", kind="data", path=padding)
                    lines[row.line] = template.rstrip("\r\n") + newline + rendered
                continue
            if change.action == "entry":
                split.name(change.neighbour, "entry name")
                entry_start = change.start
                if entry_start is None or not row.start <= entry_start < stop or entry_start % 4:
                    raise Held("boundary-map", f"{change.function}: entry start must be within its ROM interval")
                if change.neighbour in by_name:
                    raise Held("boundary-map", f"{change.function}: entry name {change.neighbour} already exists")
                directory = Path(row.path).parent
                path = (directory / change.neighbour).as_posix()
                template = lines[row.line]
                newline = row.match["newline"] or "\n"
                prefix = split.replace_row(template, row.match, kind="data", path=row.path + "_prefix")
                entry = split.replace_row(template, row.match, start=f"0x{entry_start:06X}", path=path)
                lines[row.line] = entry if entry_start == row.start else prefix.rstrip("\r\n") + newline + entry
                address = split.address(row, config.split) + entry_start - row.start
                symbol = symbols.get(change.neighbour)
                if symbol is not None and symbol[0] != address:
                    raise Held(
                        "boundary-map", f"{change.function}: entry symbol {change.neighbour} has another address"
                    )
                if symbol is None:
                    separator = "" if not symbols_after or symbols_after.endswith("\n") else newline
                    symbols_after += separator + f"{change.neighbour} = 0x{address:08X};{newline}"
                continue
            split.name(change.neighbour, "neighbour")
            owners = by_name.get(change.neighbour, [])
            if len(owners) != 1:
                raise Held("boundary-map", f"{change.function}: requires one neighbour {change.neighbour}")
            owner = owners[0]
            if owner.segment is not row.segment or owner.kind not in ("asm", "hasm", "c"):
                raise Held("boundary-map", f"{change.function}: neighbour must be text in the same segment")
            group = row.segment.rows
            lo, hi = sorted((group.index(row), group.index(owner)))
            between = group[lo + 1 : hi]
            merging = {item.function for item in items if item.action == "merge" and item.neighbour == change.neighbour}
            if any(Path(item.path).name not in merging for item in between):
                raise Held("boundary-map", f"{change.function}: nonadjacent neighbour {change.neighbour}")
            # A head merge into C would move its public entry while keeping its
            # compiled prologue. Require the operator to keep such an owner asm.
            if row.start < owner.start and owner.kind == "c":
                raise Held("boundary-map", f"{change.function}: head merge would move C entry {change.neighbour}")
            owners_by_fragment[row] = owner
            deleted.add(row)
            neighbours.add(owner)
            lines[row.line] = ""
        if neighbours & (deleted | data):
            raise Held(
                "boundary-map",
                "neighbour is also changed: "
                + ", ".join(sorted(Path(row.path).name for row in neighbours & (deleted | data))),
            )
        for owner in neighbours:
            heads = [
                row.start
                for row in deleted
                if owners_by_fragment[row] is owner
                and row.start < owner.start
                and all(
                    item in deleted
                    for item in owner.segment.rows[owner.segment.rows.index(row) : owner.segment.rows.index(owner)]
                )
            ]
            if heads:
                lines[owner.line] = split.replace_row(lines[owner.line], owner.match, start=f"0x{min(heads):06X}")
        after = "".join(lines)
        # Every original byte remains covered in the same segment and mapping.
        _, _, updated = split.parse_layout(config.split, after)
        for old, new in zip(segments, updated, strict=True):
            if (
                old.fields != new.fields
                or old.end != new.end
                or (old.rows and (not new.rows or old.rows[0].start != new.rows[0].start))
            ):
                raise Held("boundary-map", f"VERSION {version}: map changes segment ROM coverage")
        if before != after:
            edits.append(split.Edit(config.split, before, after, (version,)))
        if symbols_after != symbols_before:
            edits.append(split.Edit(config.symbols, symbols_before, symbols_after, (version,)))
    return edits


def apply(project: Project, policy: Host, changes: Sequence[Change]) -> Outcome | None:
    """Apply all versions or roll back; name refused changes on any ROM failure."""
    edits = plan(project, changes)
    try:
        results = split_apply.apply(project, policy, edits)
    except Held as error:
        names = ", ".join(f"{item.version}:{item.function}" for item in changes)
        raise Held("boundary-map", f"refused {names}; all map edits rolled back; {error.reason}") from error
    if results is not None and not results.ok:
        names = ", ".join(f"{item.version}:{item.function}" for item in changes)
        detail = "; ".join(results.lines())
        raise Held("boundary-map", f"make check refused {names}; all map edits rolled back; {detail}")
    return results
