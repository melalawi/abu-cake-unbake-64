"""Relocation-aware function correspondence and reviewable placement needs."""

from __future__ import annotations

import struct
from dataclasses import dataclass
from pathlib import Path
from re import Match
from typing import Any, cast

from unbake.decomp.needs import Need, PlacementNeed, register_deriver, register_resolver
from unbake.layout import split, xver_edits
from unbake.project.config import Held


@dataclass(frozen=True)
class Span:
    version: str
    function: str
    start: int
    end: int
    address: int
    row: str
    kind: str
    align: int | None


def _reference(project: Any) -> str:
    value = getattr(project, "names_from", None)
    if value is None:
        raise Held("placement", "project.names_from: required VERSION")
    project.version(value)
    return cast(str, value)


def _image(project: Any, version: str) -> bytes:
    path = project.version(version).baserom
    try:
        data = Path(path).read_bytes()
    except OSError as error:
        raise Held("placement", f"VERSION {version} baserom: {error}") from error
    widths = {bytes.fromhex("80371240"): 1, bytes.fromhex("37804012"): 2, bytes.fromhex("40123780"): 4}
    width = widths.get(data[:4])
    if width is None or len(data) < 64 or len(data) % width:
        raise Held("placement", f"VERSION {version} baserom: invalid byte order or size")
    if width != 1:
        data = b"".join(data[offset : offset + width][::-1] for offset in range(0, len(data), width))
    return data


def _inventory(project: Any, version: str) -> list[Any]:
    path = project.version(version).split
    _, _, segments = split.layout(path)
    rows = [
        row
        for segment in segments
        if segment.fields.get("type") == "code"
        for row in segment.rows
        if row.kind in ("asm", "c")
    ]
    if not rows:
        raise Held("placement", f"VERSION {version} text rows: required")
    return rows


def _symbol(project: Any, version: str, function: str) -> tuple[int, int, Match[str]] | None:
    _, symbols = split.symbols(project.version(version).symbols)
    return symbols.get(function)


def _named(project: Any, version: str, function: str, rows: list[Any]) -> tuple[Any, int] | None:
    symbol = _symbol(project, version, function)
    if symbol is not None:
        selected = [
            (row, row.start + symbol[0] - split.address(row, project.version(version).split))
            for row in rows
            if split.address(row, project.version(version).split)
            <= symbol[0]
            < split.address(row, project.version(version).split) + split.end(row) - row.start
        ]
    else:
        selected = [(row, row.start) for row in rows if Path(row.path).name == function]
    if len(selected) > 1:
        raise Held("placement", f"VERSION {version} function {function}: ambiguous named placement")
    return selected[0] if selected else None


def body(data: bytes, start: int, end: int, function: str) -> list[int]:
    if start < 0 or end > len(data) or start >= end or start % 4 or end % 4:
        raise Held("placement", f"function {function} start/end: invalid word range")
    words = list(struct.unpack(f">{(end - start) // 4}I", data[start:end]))
    returns = [index + 2 for index, word in enumerate(words[:-1]) if word == 0x03E00008]
    if returns and not any(words[returns[-1] :]):
        words = words[: returns[-1]]
    if not words:
        raise Held("placement", f"function {function} words: required")
    return words


def _masks(words: list[int]) -> list[int]:
    masks = [0x03FFFFFF if word >> 26 in (2, 3) else 0 for word in words]
    pending = {}
    low_ops = {9, 13, 32, 33, 35, 36, 37, 40, 41, 43, 49, 53, 57, 61}
    for index, word in enumerate(words):
        op, rs, rt = word >> 26, word >> 21 & 31, word >> 16 & 31
        if op == 15 and rt:
            pending[rt] = index
            continue
        if op in low_ops and rs in pending:
            masks[pending[rs]] = 0xFFFF
            masks[index] = 0xFFFF
        # Stop following an address register after an instruction overwrites it.
        destination = (
            word >> 11 & 31 if op == 0 else rt if op in {8, 9, 10, 11, 12, 13, 14, 32, 33, 35, 36, 37} else None
        )
        if destination is not None and destination in pending:
            del pending[destination]
    return masks


def _equal(words: list[int], masks: list[int], data: bytes, start: int) -> bool:
    if start % 4 or start < 0 or start + len(words) * 4 > len(data):
        return False
    return all(
        (actual ^ wanted) & (~mask & 0xFFFFFFFF) == 0
        for wanted, mask, (actual,) in zip(
            words, masks, struct.iter_unpack(">I", data[start : start + len(words) * 4]), strict=False
        )
    )


def _span(project: Any, version: str, function: str, row: Any, start: int, words: list[int], data: bytes) -> Span:
    end = start + len(words) * 4
    limit = split.end(row)
    if end > limit:
        raise Held("placement", f"VERSION {version} function {function}: crosses row boundary")
    alignment = None
    if end < limit and not any(data[end:limit]):
        value = row.segment.fields.get("subalign")
        if value is None:
            raise Held("placement", f"VERSION {version} function {function} subalign: required for padding")
        value = split.number(value, "subalign")
        if value == 0 or value & (value - 1):
            raise Held("placement", f"VERSION {version} function {function} subalign: required power of two")
        address = split.address(row, project.version(version).split) + end - row.start
        if (address + value - 1) // value * value == address + limit - end:
            alignment, end = value, limit
    address = split.address(row, project.version(version).split) + start - row.start
    return Span(version, function, start, end, address, row.path, row.kind, alignment)


def locate(project: Any, function: str) -> dict[str, Span | None]:
    """Find one relocation-masked text span per VERSION; refuse ambiguous matches."""
    function = split.name(function)
    reference = _reference(project)
    rows = _inventory(project, reference)
    named = _named(project, reference, function, rows)
    if named is None:
        raise Held("placement", f"VERSION {reference} function {function}: missing reference placement")
    row, start = named
    image = _image(project, reference)
    words = body(image, start, split.end(row), function)
    masks = _masks(words)
    fixed = [(index, word) for index, (word, mask) in enumerate(zip(words, masks, strict=False)) if not mask]
    if not fixed:
        raise Held("placement", f"function {function} relocation signature: no fixed instruction")
    anchor_index, anchor = fixed[0]
    result: dict[str, Span | None] = {}
    for version in project.versions:
        data = image if version == reference else _image(project, version)
        rows = _inventory(project, version)
        named = _named(project, version, function, rows)
        if named is not None:
            target_row, at = named
            # An explicit VERSION placement can contain real code differences.
            target_body = body(data, at, split.end(target_row), function)
            result[version] = _span(project, version, function, target_row, at, target_body, data)
            continue
        candidates = []
        for target_row in rows:
            at = target_row.start
            limit = split.end(target_row)
            while True:
                found = data.find(anchor.to_bytes(4, "big"), at, limit)
                if found < 0:
                    break
                at = found + 1
                candidate = found - anchor_index * 4
                if candidate < target_row.start or candidate + len(words) * 4 > limit:
                    continue
                if _equal(words, masks, data, candidate):
                    candidates.append(_span(project, version, function, target_row, candidate, words, data))
        if len(candidates) > 1:
            raise Held("placement", f"VERSION {version} function {function}: ambiguous relocation-masked placement")
        result[version] = candidates[0] if candidates else None
    return result


def _placement(project: Any, span: Span) -> list[PlacementNeed]:
    from unbake.decomp.needs import PlacementNeed

    evidence = f"VERSION {span.version} text at ROM 0x{span.start:X}..0x{span.end:X}"
    row = next(row for row in _inventory(project, span.version) if row.start <= span.start < split.end(row))
    result = []
    if row.start != span.start or split.end(row) != span.end:
        if row.kind != "asm":
            raise Held("placement", f"VERSION {span.version} function {span.function}: cannot cut c row {row.path}")
        result.append(PlacementNeed(span.version, span.function, span.start, span.end, "cut", span.address, evidence))
    elif Path(row.path).name != span.function:
        if row.kind != "asm":
            raise Held("placement", f"VERSION {span.version} function {span.function}: cannot rename c row {row.path}")
        result.append(
            PlacementNeed(span.version, span.function, span.start, span.end, "rename", Path(row.path).name, evidence)
        )
    symbol = _symbol(project, span.version, span.function)
    if symbol is None:
        result.append(PlacementNeed(span.version, span.function, span.start, span.end, "place", span.address, evidence))
    elif symbol[0] != span.address:
        raise Held("placement", f"VERSION {span.version} symbol {span.function}: conflicting address")
    if span.align is not None:
        result.append(PlacementNeed(span.version, span.function, span.start, span.end, "align", span.align, evidence))
    return result


def needs(project: Any, function: str, trial: Any) -> list[PlacementNeed]:
    """Plan caller placements and the function symbols requested by a trial."""
    from unbake.decomp.needs import SymbolNeed

    if trial is None:
        raise Held("placement", "trial: required")
    if trial.function != function:
        raise Held("placement", f"trial.function: expected {function}")
    result = []
    for span in locate(project, function).values():
        if span is not None:
            result.extend(_placement(project, span))
    requested = getattr(trial, "needs", None)
    if requested is None:
        raise Held("placement", "trial.needs: required")
    for need in requested:
        if not isinstance(need, SymbolNeed) or need.type != "func":
            continue
        if need.address is None:
            raise Held("placement", f"symbol {need.name} address: required")
        spans = locate(project, need.name)
        span = spans.get(need.version)
        if span is None or span.address != need.address + need.addend:
            raise Held("placement", f"VERSION {need.version} callee {need.name}: no proved twin at requested address")
        result.extend(_placement(project, span))
    return list(dict.fromkeys(result))


def twins(project: Any) -> list[PlacementNeed]:
    """Return placement work for named functions linked from C in any VERSION."""
    _reference(project)
    names = sorted(
        {
            function.name
            for version in project.versions
            for function in split.functions(project, version)
            if function.kind == "c"
        }
    )
    return [
        need
        for name in names
        for span in locate(project, name).values()
        if span is not None
        for need in _placement(project, span)
    ]


def derive(context: Any) -> list[Need]:
    """Decode called function symbols from trial objects and plan their text twins."""
    from types import SimpleNamespace

    from unbake.decomp.needs import SymbolNeed
    from unbake.project_tools.elf import Object

    requested = []
    for version, artifact in context.artifacts.items():
        for field in ("unit", "target_words", "span"):
            if field not in artifact:
                raise Held("placement", f"artifacts.{version}.{field}: required")
        try:
            obj = Object(artifact["unit"].path)
            section = obj.section(".text")
            if section is None:
                raise Held("placement", f"artifacts.{version}.text: required")
            for offset, kind, symbol in obj.relocations(section):
                if kind != 4 or symbol["section"] != 0:
                    continue
                name = symbol["name"]
                if offset % 4 or offset // 4 >= len(artifact["target_words"]):
                    raise Held("placement", f"callee {name} offset: missing target word")
                word = artifact["target_words"][offset // 4]
                if "rodata" in artifact:
                    word = artifact["rodata"].target_words.get(offset)
                    if word is None:
                        raise Held("placement", f"callee {name} offset: missing aligned target word")
                if word >> 26 not in (2, 3):
                    # A shifted draft proves no callee address here; the comparison reports the shift.
                    continue
                addend = struct.unpack_from(">I", obj.content(section), offset)[0] & 0x03FFFFFF
                if addend:
                    raise Held("placement", f"callee {name} addend: nonzero function entry")
                address = (artifact["span"].address + offset + 4) & 0xF0000000 | (word & 0x03FFFFFF) << 2
                span = locate(context.project, name).get(version)
                if span is None or span.address != address:
                    raise Held("placement", f"VERSION {version} callee {name}: no proved twin at 0x{address:X}")
                requested.append(
                    SymbolNeed(
                        version,
                        name,
                        address,
                        0,
                        ".text",
                        "func",
                        span.end - span.start,
                        f"R_MIPS_26 text+0x{offset:X}",
                    )
                )
        except (OSError, ValueError, IndexError, struct.error) as error:
            raise Held("placement", f"artifacts.{version}.unit: {error}") from error
    trial = SimpleNamespace(function=context.trial.function, needs=requested)
    result: list[Need] = list(dict.fromkeys(requested))
    result.extend(needs(context.project, trial.function, trial))
    return result


register_resolver(PlacementNeed, 50, xver_edits.resolve)
register_deriver(derive)
