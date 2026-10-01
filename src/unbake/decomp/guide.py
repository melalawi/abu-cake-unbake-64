"""Exact declarations and data placements for target instructions and trial needs."""

from __future__ import annotations

import shlex
import struct
from collections.abc import Iterable, Sequence

from unbake.decomp.commands import prefix
from unbake.decomp.draft_input import version_for
from unbake.decomp.indexed import table_guidance
from unbake.decomp.needs import LayoutNeed, Need, SymbolNeed
from unbake.decomp.symbols import Binding, DataRow, TrialElf, derive, symbol_line
from unbake.families import Family
from unbake.project.config import Held, Project, load_policy

_C_TYPES = {
    "f32": "f32",
    "f64": "f64",
    "s8": "s8",
    "u8": "u8",
    "s16": "s16",
    "u16": "u16",
    "s32": "s32",
    "u32": "u32",
    "s64": "s64",
    "u64": "u64",
}
_SIZES = {"f32": 4, "f64": 8, "s8": 1, "u8": 1, "s16": 2, "u16": 2, "s32": 4, "u32": 4, "s64": 8, "u64": 8}


def extern(need: SymbolNeed) -> str:
    """Give the exact scalar/array declaration justified by type and extent."""
    symbol_line(need)
    if need.type not in _C_TYPES:
        raise Held("guide", f"{need.name}.type: no exact C declaration for {need.type}")
    width = _SIZES[need.type]
    if need.size % width:
        raise Held("guide", f"{need.name}.size: not divisible by {need.type} width")
    extent = "" if need.size == width else f"[{need.size // width}]"
    return f"extern {_C_TYPES[need.type]} {need.name}{extent};"


def render(needs: Iterable[Need]) -> str:
    """Render declarations and exact VERSION symbol-file placement for try output."""
    lines, seen = [], set()
    for need in needs:
        if isinstance(need, LayoutNeed):
            lines.append(
                f"need: {need.struct} (LayoutNeed), VERSION {need.version}: shared paths.include declaration required"
            )
            lines.append(f"source: {need.source}; fields: {need.fields}")
            continue
        if not isinstance(need, SymbolNeed):
            continue
        key = (need.version, need.name, need.address, need.type, need.size)
        if key in seen:
            continue
        seen.add(key)
        lines.append(extern(need))
        lines.append(f"placement: versions/{need.version}/symbol_addrs.txt: {symbol_line(need)}")
        lines.append(f"reference: {need.section} 0x{need.address:08X} + {need.addend}; {need.evidence}")
    return "\n".join(lines)


def from_words(
    target_words: Sequence[int],
    version: str,
    bindings: Iterable[Binding],
    rows: Iterable[DataRow],
    gp: int | None,
    family: Family,
) -> list[Need]:
    """Derive guidance before drafting, using the same constant-reference analysis."""
    evidence = TrialElf(tuple(target_words), (), tuple(bindings), tuple(rows), gp, family, frozenset())
    return derive(evidence, target_words, version)


def prologue(target_words: Iterable[int]) -> str:
    """Report only frame size and saved registers explicitly visible in instructions."""
    lines = []
    for index, word in enumerate(target_words):
        op, rs, rt, immediate = word >> 26, word >> 21 & 31, word >> 16 & 31, word & 0xFFFF
        signed = (immediate & 0x7FFF) - (immediate & 0x8000)
        if op == 9 and rs == rt == 29 and signed < 0:
            lines.append(f"frame: 0x{-signed:X} bytes")
        elif op in (0x2B, 0x3F) and rs == 29 and rt in (*range(16, 24), 30, 31):
            name = "ra" if rt == 31 else "fp" if rt == 30 else f"s{rt - 16}"
            lines.append(f"saved: {name} at sp{signed:+d}")
        elif index and op not in (0, 9, 15, 0x2B, 0x3F, 0x11):
            break
    return "\n".join(lines)


def words(data: bytes, byteorder: str) -> tuple[int, ...]:
    """Decode explicit big/little endian instruction words; refuse partial tails."""
    if byteorder not in ("big", "little"):
        raise Held("symbols", "byteorder: expected big or little")
    if len(data) % 4:
        raise Held("symbols", "target_words: incomplete word")
    return tuple(item[0] for item in struct.iter_unpack(">I" if byteorder == "big" else "<I", data))


def data_rows(project: Project, version: str) -> tuple[DataRow, ...]:
    """Read resident intervals, using the split's explicit VRAM BSS end."""
    from unbake.layout import split

    configured = project.version(version)
    _, _, segments = split.layout(configured.split)
    result = []
    for segment in segments:
        for index, row in enumerate(segment.rows):
            kind = row.kind.lstrip(".")
            if kind not in ("data", "rodata", "rdata", "bss"):
                continue
            start = split.address(row, configured.split)
            if kind == "bss":
                following = segment.rows[index + 1 :]
                end = (
                    split.address(following[0], configured.split)
                    if following
                    else split.bss_end(project, version, segment.fields["name"])
                )
            else:
                end = start + split.end(row) - row.start
            result.append(DataRow(row.path, start, end, "." + kind))
    return tuple(result)


def run(project: Project, function: str, version: str | None) -> str:
    """Read a configured function's target and print data declarations and prologue."""
    from unbake.decomp import trial_layout

    version = version_for(project, version, "guide")
    configured = project.version(version)
    values = trial_layout.symbol_values(configured.symbols)
    span = trial_layout.function_span(configured, function, values)
    if span is None:
        raise Held("guide", f"{function}: missing split placement in VERSION {version}")
    from unbake.families import family_for

    family = family_for(project.compiler_for(project.src / (function + ".c")).id)
    rows = data_rows(project, version)
    target = words(trial_layout.target(configured, span), "big")
    inferred = from_words(target, version, (), rows, values.get("_gp"), family)
    bindings = []
    for need in inferred:
        if isinstance(need, SymbolNeed):
            aliases = [name for name, address in values.items() if address == need.address]
            if len(aliases) == 1:
                bindings.append(Binding(aliases[0], need.address, need.section, need.type, need.size))
    needs = from_words(target, version, bindings, rows, values.get("_gp"), family)
    from unbake.decomp.drafts import Store

    store = Store(load_policy(), project)
    rows_history = store.rows(function)
    commands = []
    if rows_history:
        from unbake.decomp.needs import decode

        latest = rows_history[-1]
        pending = [decode(item) for item in latest["needs"]]
        needs.extend(need for need in pending if isinstance(need, LayoutNeed) and need.version == version)
        for need in needs:
            if isinstance(need, LayoutNeed):
                command = [
                    "python",
                    "-m",
                    "unbake.decomp.declarations",
                    *prefix(project)[1:],
                    "--source",
                    need.source,
                    "--version",
                    version,
                ]
                commands.append("resolve: " + shlex.join(command))
    output = "\n".join(
        part
        for part in (
            prologue(target),
            render(needs),
            table_guidance(project, version, function, span, target),
            *dict.fromkeys(commands),
        )
        if part
    )
    if not output:
        output = (
            f"{function}, VERSION {version}: no data or frame needs inferred; "
            "shared struct field accesses require declarations in paths.include; run "
            + shlex.join(
                [
                    *prefix(project),
                    "decomp",
                    "draft",
                    function,
                    "--version",
                    version,
                    "--scratch",
                    str(project.root.parent / (project.name + "-drafts")),
                ]
            )
        )
    print(output)
    return output
