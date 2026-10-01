"""Plan reviewable edits to layout rows and symbol names."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, TypedDict, cast

from unbake.layout import split, split_apply
from unbake.project.config import Held

if TYPE_CHECKING:
    from unbake.project.config import Project


class MatchedRow(TypedDict):
    function: str
    versions: list[str]


def align(project: Project, v: str, function: str, value: object) -> list[split.Edit]:
    """Record proved trailing text alignment on one named split row."""
    function = split.name(function)
    alignment = split.number(value, "align")
    if alignment == 0 or alignment & (alignment - 1):
        raise Held("split", "align: required positive power of two")
    version = project.version(v)
    before, lines, segments = split.layout(version.split)
    rows = [
        row
        for segment in segments
        for row in segment.rows
        if row.kind in ("asm", "c") and Path(row.path).name == function
    ]
    if len(rows) != 1:
        raise Held("split", f"function {function}: align requires one text row in VERSION {v}")
    row = rows[0]
    if row.match["align"] is not None:
        current = split.number(row.match["align"], "align")
        if current != alignment:
            raise Held("split", f"function {function} align: conflicts with {current}")
        return []
    at = row.match.end("path")
    lines[row.line] = lines[row.line][:at] + f", {{align: {alignment}}}" + lines[row.line][at:]
    return [split.Edit(version.split, before, "".join(lines), (v,))]


def place_text(path: Path, function: str, address: int) -> tuple[str, str]:
    before, symbols = split.symbols(path)
    if function in symbols:
        if symbols[function][0] != address:
            raise Held("split", f"{path}: symbol {function} already at {symbols[function][0]:#x}")
        return before, before
    newline = "\r\n" if "\r\n" in before else "\n"
    separator = newline if before and not before.endswith("\n") else ""
    return before, before + separator + f"{function} = 0x{address:08X};{newline}"


def place(project: Project, v: str, function: str, address: object) -> list[split.Edit]:
    function = split.name(function)
    address_value = split.number(address, "address")
    version = project.version(v)
    before, after = place_text(version.symbols, function, address_value)
    return [split.Edit(version.symbols, before, after, (v,))] if before != after else []


def _cut(project: Project, v: str, function: str, start: object, end: object, kind: str) -> list[split.Edit]:
    function = split.name(function)
    begin, stop_at = split.number(start, "start"), split.number(end, "end")
    if begin >= stop_at or begin % 4 or stop_at % 4:
        raise Held("split", "start/end: required increasing word-aligned ROM offsets")
    version = project.version(v)
    before, lines, segments = split.layout(version.split)
    selected = [row for segment in segments for row in segment.rows if row.start <= begin < split.end(row)]
    if len(selected) != 1:
        raise Held("split", f"{version.split}: start {begin:#x} must select one row")
    row = selected[0]
    stop = split.end(row)
    if row.kind != "asm" or stop_at > stop:
        raise Held("split", f"{version.split}: start/end must lie within one asm row")
    paths = {item.path for segment in segments for item in segment.rows if item is not row}
    directory = str(Path(row.path).parent)
    target = function if directory == "." else f"{directory}/{function}"
    if target in paths or (begin > row.start and target == row.path):
        raise Held("split", f"{version.split}: function {function} already has a row")
    rendered = []
    newline = row.match["newline"] or ("\r\n" if "\r\n" in before else "\n")
    template = lines[row.line]
    if not row.match["newline"]:
        template += newline
    if begin > row.start:
        rendered.append(template)
    rendered.append(split.replace_row(template, row.match, start=f"0x{begin:06X}", kind=kind, path=target))
    if stop_at < stop:
        suffix = f"{row.path}_{stop_at:06X}"
        if suffix in paths:
            raise Held("split", f"{version.split}: suffix row {suffix} already exists")
        rendered.append(split.replace_row(template, row.match, start=f"0x{stop_at:06X}", path=suffix))
    if not row.match["newline"]:
        rendered[-1] = rendered[-1].removesuffix(newline)
    lines[row.line] = "".join(rendered)
    edits = [split.Edit(version.split, before, "".join(lines), (v,))]
    address = split.address(row, version.split) + begin - row.start
    edits.extend(place(project, v, function, address))
    return [edit for edit in edits if edit.before != edit.after]


def cut(project: Project, v: str, function: str, start: object, end: object) -> list[split.Edit]:
    return _cut(project, v, function, start, end, "asm")


def data_cut(project: Project, v: str, function: str, start: object, end: object) -> list[split.Edit]:
    return _cut(project, v, function, start, end, "data")


def rename_version(project: Project, v: str, function: str, new_name: str) -> list[split.Edit]:
    version = project.version(v)
    before, lines, segments = split.layout(version.split)
    symbols_before, symbols = split.symbols(version.symbols)
    symbol = symbols.get(function)
    rows = [
        row
        for segment in segments
        for row in segment.rows
        if Path(row.path).name == function or (symbol is not None and split.address(row, version.split) == symbol[0])
    ]
    if not rows and symbol is None:
        return []
    if new_name in symbols or any(Path(row.path).name == new_name for segment in segments for row in segment.rows):
        raise Held("split", f"VERSION {v}: new_name {new_name} already exists")
    for row in rows:
        renamed = str(Path(row.path).with_name(new_name))
        lines[row.line] = split.replace_row(lines[row.line], row.match, path=renamed)
    edits = []
    after = "".join(lines)
    if before != after:
        edits.append(split.Edit(version.split, before, after, (v,)))
    if symbol is not None:
        symbol_lines = symbols_before.splitlines(keepends=True)
        _, index, match = symbol
        line = symbol_lines[index]
        symbol_lines[index] = line[: match.start("name")] + new_name + line[match.end("name") :]
        edits.append(split.Edit(version.symbols, symbols_before, "".join(symbol_lines), (v,)))
    return edits


def rename(project: Project, function: str, new_name: str) -> list[split.Edit]:
    function, new_name = split.name(function), split.name(new_name, "new_name")
    if function == new_name:
        raise Held("split", "new_name: must differ from function")
    from unbake.layout.data_symbols import counterparts

    text_names = {item for v in project.versions for row in split.functions(project, v) for item in row.aliases}
    present = [v for v in project.versions if function in split.symbols(project.version(v).symbols)[1]]
    names = (
        {}
        if function in text_names or not present or len(present) == len(project.versions)
        else counterparts(project, function)
    )
    edits = [edit for v in project.versions for edit in rename_version(project, v, names.get(v, function), new_name)]
    if not edits:
        raise Held("split", f"function {function}: not present in any VERSION")
    return edits


def matched(project: Project) -> list[MatchedRow]:
    path = project.root / "data" / "matched.jsonl"
    if not path.exists():
        return []
    result: list[MatchedRow] = []
    for index, line in enumerate(split.read(path).splitlines()):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except ValueError as exc:
            raise Held("split", f"{path}:{index + 1}: JSON") from exc
        if (
            not isinstance(row, dict)
            or not isinstance(row.get("function"), str)
            or not isinstance(row.get("versions"), list)
        ):
            raise Held("split", f"{path}:{index + 1}: function/versions")
        result.append(cast(MatchedRow, row))
    return result


def twins(project: Project, v: str, function: str) -> list[split.Edit]:
    function = split.name(function)
    project.version(v)
    matches = matched(project)
    if not any(row["function"] == function and v in row["versions"] for row in matches):
        raise Held("split", f"function {function}: not matched in VERSION {v}")
    source = [item for item in split.functions(project, v) if function in item.aliases]
    if len(source) != 1 or source[0].kind != "c":
        raise Held("split", f"function {function}: requires one c row in VERSION {v}")
    words = split.words(project, source[0])
    edits = []
    for other in project.versions:
        if other == v:
            continue
        candidates = [
            item
            for item in split.functions(project, other)
            if item.kind == "asm"
            and item.end - item.start == len(words)
            and not any(row["function"] in item.aliases and other in row["versions"] for row in matches)
            and split.words(project, item) == words
        ]
        if len(candidates) > 1:
            raise Held("split", f"VERSION {other}: ambiguous twins for {function}")
        if candidates and function not in candidates[0].aliases:
            candidate = candidates[0]
            edits.extend(rename_version(project, other, candidate.name, function))
    return split_apply.coalesce(edits)
