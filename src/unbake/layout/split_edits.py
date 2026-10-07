"""Plan reviewable edits to layout rows and symbol names."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from unbake.config import Held
from unbake.layout import split
from unbake.process import named as cause_named

if TYPE_CHECKING:
    from unbake.config import Host, Project


def align(project: Project, v: str, function: str, value: object) -> list[split.Edit]:
    """Record proved trailing text alignment on one named split row."""
    function = split.name(function)
    alignment = split.number(value, "align")
    if alignment == 0 or alignment & (alignment - 1):
        raise Held(
            cause_named("align", "align: required positive power of two", owner="layout.split_edits", stage="split")
        )
    version = project.version(v)
    before, lines, segments = split.layout(version.split)
    rows = [
        row
        for segment in segments
        for row in segment.rows
        if row.kind in split.CODE_KINDS and Path(row.path).name == function
    ]
    if len(rows) != 1:
        raise Held(
            cause_named(
                "layout.split_edits.align",
                f"function {function}: align requires one text row in VERSION {v}",
                owner="layout.split_edits",
                stage="split",
            )
        )
    row = rows[0]
    if row.match["align"] is not None:
        current = split.number(row.match["align"], "align")
        if current != alignment:
            raise Held(
                cause_named(
                    "layout.split_edits.align",
                    f"function {function} align: conflicts with {current}",
                    owner="layout.split_edits",
                    stage="split",
                )
            )
        return []
    at = row.match.end("path")
    lines[row.line] = lines[row.line][:at] + f", {{align: {alignment}}}" + lines[row.line][at:]
    return [split.Edit(version.split, before, "".join(lines), (v,))]


def place_text(path: Path, function: str, address: int) -> tuple[str, str]:
    before, symbols = split.symbols(path)
    if function in symbols:
        if symbols[function][0] != address:
            raise Held(
                cause_named(
                    f"{path}",
                    f"{path}: symbol {function} already at {symbols[function][0]:#x}",
                    owner="layout.split_edits",
                    stage="split",
                )
            )
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


def _cut(
    project: Project, v: str, function: str, start: object, end: object, kind: str, *, data_owner: bool = False
) -> list[split.Edit]:
    function = split.name(function)
    begin, stop_at = split.number(start, "start"), split.number(end, "end")
    if begin >= stop_at or begin % 4 or stop_at % 4:
        raise Held(
            cause_named(
                "start/end",
                "start/end: required increasing word-aligned ROM offsets",
                owner="layout.split_edits",
                stage="split",
            )
        )
    version = project.version(v)
    before, lines, segments = split.layout(version.split)
    selected = [row for segment in segments for row in segment.rows if row.start <= begin < split.end(row)]
    if len(selected) != 1:
        raise Held(
            cause_named(
                "split.cut.selection",
                f"split.cut.selection: start {begin:#x} must select one row",
                owner="layout.split_edits",
                stage="split",
            )
        )
    row = selected[0]
    stop = split.end(row)
    if row.kind != "asm" and not (data_owner and row.kind in ("data", "rodata", "rdata")):
        if row.kind in ("data", "rodata", "rdata"):
            raise Held(
                cause_named(
                    "split.data_to_code",
                    "split.data_to_code: use unbake split code FUNCTION --version V --start ROM --end ROM",
                    owner="layout.split_edits",
                    stage="split",
                )
            )
        raise Held(
            cause_named(
                "split.cut.owner",
                "split.cut.owner: start/end require an asm row; C needs an explicit text boundary correction",
                owner="layout.split_edits",
                stage="split",
            )
        )
    if stop_at > stop:
        raise Held(
            cause_named(
                "split.cut.interval",
                "split.cut.interval: start/end must lie within one row",
                owner="layout.split_edits",
                stage="split",
            )
        )
    paths = {item.path for segment in segments for item in segment.rows if item is not row}
    directory = "." if data_owner else str(Path(row.path).parent)
    target = function if directory == "." else f"{directory}/{function}"
    if target in paths or (begin > row.start and target == row.path):
        raise Held(
            cause_named(
                f"{version.split}",
                f"{version.split}: function {function} already has a row",
                owner="layout.split_edits",
                stage="split",
            )
        )
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
            raise Held(
                cause_named(
                    f"{version.split}",
                    f"{version.split}: suffix row {suffix} already exists",
                    owner="layout.split_edits",
                    stage="split",
                )
            )
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


def code(project: Project, v: str, function: str, start: object, end: object, *, policy: Host) -> list[split.Edit]:
    from unbake.layout.code_interval import prove

    split.name(function)
    prove(project, v, split.number(start, "start"), split.number(end, "end"), policy)
    return _cut(project, v, function, start, end, "asm", data_owner=True)


def data_cut(project: Project, v: str, function: str, start: object, end: object) -> list[split.Edit]:
    return _cut(project, v, function, start, end, "data")
