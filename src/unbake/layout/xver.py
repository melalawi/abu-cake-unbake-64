"""Relocation-aware function correspondence and reviewable placement needs."""

from __future__ import annotations

import struct
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from re import Match
from typing import Any, cast

from unbake.config import Held
from unbake.decomp.needs import PlacementNeed, register_resolver
from unbake.layout import split, xver_edits
from unbake.process import capture
from unbake.process import named as cause_named


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
        raise Held(
            cause_named(
                "project.names_from", "project.names_from: required VERSION", owner="layout.xver", stage="placement"
            )
        )
    project.version(value)
    return cast(str, value)


def _image(project: Any, version: str) -> bytes:
    path = project.version(version).baserom
    try:
        data = Path(path).read_bytes()
    except OSError as error:
        raise Held(
            capture(
                error,
                cause=cause_named(
                    "layout.xver._image", f"VERSION {version} baserom: {error}", owner="layout.xver", stage="placement"
                ),
            )
        ) from error
    widths = {bytes.fromhex("80371240"): 1, bytes.fromhex("37804012"): 2, bytes.fromhex("40123780"): 4}
    width = widths.get(data[:4])
    if width is None or len(data) < 64 or len(data) % width:
        raise Held(
            cause_named(
                "layout.xver._image",
                f"VERSION {version} baserom: invalid byte order or size",
                owner="layout.xver",
                stage="placement",
            )
        )
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
        if row.kind in split.CODE_KINDS
    ]
    if not rows:
        raise Held(
            cause_named(
                "layout.xver._inventory",
                f"VERSION {version} text rows: required",
                owner="layout.xver",
                stage="placement",
            )
        )
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
        raise Held(
            cause_named(
                "layout.xver._named",
                f"VERSION {version} function {function}: ambiguous named placement",
                owner="layout.xver",
                stage="placement",
            )
        )
    return selected[0] if selected else None


def body(data: bytes, start: int, end: int, function: str) -> list[int]:
    if start < 0 or end > len(data) or start >= end or start % 4 or end % 4:
        raise Held(
            cause_named(
                "layout.xver.body",
                f"function {function} start/end: invalid word range",
                owner="layout.xver",
                stage="placement",
            )
        )
    words = list(struct.unpack(f">{(end - start) // 4}I", data[start:end]))
    returns = [index + 2 for index, word in enumerate(words[:-1]) if word == 0x03E00008]
    if returns and not any(words[returns[-1] :]):
        words = words[: returns[-1]]
    if not words:
        raise Held(
            cause_named(
                "layout.xver.body", f"function {function} words: required", owner="layout.xver", stage="placement"
            )
        )
    return words


def _masks(words: list[int]) -> list[int]:
    """Mask paired N64 address halves, retaining provenance through indexing.

    Unresolved high halves flow through addu indexing. Completed addiu/ori
    addresses consume their high half; later arithmetic immediates are offsets,
    never relocations. Integer/float constants outside the direct-mapped
    address range stay fixed, as do unpaired high halves.
    """
    masks = [0x03FFFFFF if word >> 26 in (2, 3) else 0 for word in words]
    # Register -> (lui index, low-half index or None, indexed).
    pending: dict[int, tuple[int, int | None, bool]] = {}
    memory_ops = {32, 33, 34, 35, 36, 37, 38, 39, 40, 41, 42, 43, 44, 45, 46, 48, 49, 52, 53, 55, 56, 57, 60, 61, 63}
    for index, word in enumerate(words):
        op, rs, rt, rd = word >> 26, word >> 21 & 31, word >> 16 & 31, word >> 11 & 31
        if op == 15 and rt:
            pending.pop(rt, None)
            if 0x8000 <= word & 0xFFFF <= 0xC000:
                pending[rt] = (index, None, False)
            continue
        source = pending.get(rs)
        if source is not None and (op in memory_ops or (op == 0 and word & 63 in (8, 9))):
            high_index, low_index, _ = source
            if low_index is not None:
                masks[high_index] = masks[low_index] = 0xFFFF
            elif op in memory_ops:
                high = words[high_index] << 16 & 0xFFFFFFFF
                low = word & 0xFFFF
                address = (high + (low & 0x7FFF) - (low & 0x8000)) & 0xFFFFFFFF
                if 0x80000000 <= address < 0xC0000000:
                    masks[high_index] = masks[index] = 0xFFFF
        if op in (9, 13):
            pending.pop(rt, None)
            if rt and source is not None:
                high_index, low_index, indexed = source
                # An immediate added to a dynamic index is not proved to be
                # the symbol's low half. Leave that arithmetic significant.
                if low_index is None and not indexed:
                    high = words[high_index] << 16 & 0xFFFFFFFF
                    low = word & 0xFFFF
                    address = high | low if op == 13 else (high + (low & 0x7FFF) - (low & 0x8000)) & 0xFFFFFFFF
                    if 0x80000000 <= address < 0xC0000000:
                        masks[high_index] = masks[index] = 0xFFFF
                        pending[rt] = (high_index, index, False)
                elif low_index is not None and op == 9:
                    pending[rt] = source
            continue
        if op == 0 and word & 63 == 33:
            sources = [pending[r] for r in (rs, rt) if r in pending]
            pending.pop(rd, None)
            if rd and len(sources) == 1:
                high_index, low_index, _ = sources[0]
                pending[rd] = (high_index, low_index, True)
            continue
        # Stores and floating-point loads do not overwrite their GPR base.
        destination = (
            rd
            if op == 0
            else 31
            if op == 3
            else rt
            if op in {8, 10, 11, 12, 14, 24, 25, 26, 27, 32, 33, 34, 35, 36, 37, 38, 39, 48, 52, 55, 56, 60}
            or (op in (16, 17, 18) and rs in (0, 1, 2))
            else None
        )
        if destination is not None:
            pending.pop(destination, None)
    return masks


def _equal(words: list[int], masks: list[int], data: bytes, start: int) -> bool:
    if start % 4 or start < 0 or start + len(words) * 4 > len(data):
        return False
    actual_words = list(struct.unpack(f">{len(words)}I", data[start : start + len(words) * 4]))
    return _masks(actual_words) == masks and all(
        (actual ^ wanted) & (~mask & 0xFFFFFFFF) == 0
        for wanted, mask, actual in zip(words, masks, actual_words, strict=True)
    )


def _span(project: Any, version: str, function: str, row: Any, start: int, words: list[int], data: bytes) -> Span:
    end = start + len(words) * 4
    limit = split.end(row)
    if end > limit:
        raise Held(
            cause_named(
                "layout.xver._span",
                f"VERSION {version} function {function}: crosses row boundary",
                owner="layout.xver",
                stage="placement",
            )
        )
    alignment = None
    if end < limit and not any(data[end:limit]):
        value = row.segment.fields.get("subalign")
        if value is None:
            raise Held(
                cause_named(
                    "layout.xver._span",
                    f"VERSION {version} function {function} subalign: required for padding",
                    owner="layout.xver",
                    stage="placement",
                )
            )
        value = split.number(value, "subalign")
        if value == 0 or value & (value - 1):
            raise Held(
                cause_named(
                    "layout.xver._span",
                    f"VERSION {version} function {function} subalign: required power of two",
                    owner="layout.xver",
                    stage="placement",
                )
            )
        address = split.address(row, project.version(version).split) + end - row.start
        if (address + value - 1) // value * value == address + limit - end:
            alignment, end = value, limit
        elif start == row.start and Path(row.path).name == function:
            # A named row already owns its zero tail, including explicit object
            # padding beyond subalign. Trimming a signature must not split it.
            end = limit
    address = split.address(row, project.version(version).split) + start - row.start
    return Span(version, function, start, end, address, row.path, row.kind, alignment)


def _jump(word: int, address: int) -> int:
    return ((address + 4) & 0xF0000000) | ((word & 0x03FFFFFF) << 2)


def _disambiguate(
    project: Any,
    function: str,
    reference: str,
    origin: int,
    words: list[int],
    image: bytes,
    version: str,
    data: bytes,
    candidates: list[Span],
) -> list[Span]:
    """Constrain twins by named jump operands and structurally identical callers."""
    from unbake.decomp.symbols import references

    _, source_symbols = split.symbols(project.version(reference).symbols)
    _, target_symbols = split.symbols(project.version(version).symbols)
    addresses: dict[int, set[int]] = {}
    for name, (address, _, _) in source_symbols.items():
        if name in target_symbols:
            addresses.setdefault(address, set()).add(target_symbols[name][0])
    for index, word in enumerate(words):
        if word >> 26 not in (2, 3):
            continue
        expected = addresses.get(_jump(word, origin + index * 4))
        if expected and len(expected) == 1:
            candidates = [
                span
                for span in candidates
                if _jump(
                    int.from_bytes(data[span.start + index * 4 : span.start + index * 4 + 4], "big"),
                    span.address + index * 4,
                )
                in expected
            ]
    for operand in references(words, source_symbols.get("_gp", (None,))[0]):
        expected = addresses.get(operand.address)
        if not expected or len(expected) != 1:
            continue
        retained = []
        for span in candidates:
            actual = list(struct.unpack(f">{len(words)}I", data[span.start : span.start + len(words) * 4]))
            target_operands = references(actual, target_symbols.get("_gp", (None,))[0])
            if any(value.offset == operand.offset and value.address in expected for value in target_operands):
                retained.append(span)
        candidates = retained
    if len(candidates) <= 1:
        return candidates
    target_rows = _inventory(project, version)
    for row in _inventory(project, reference):
        caller_address = split.address(row, project.version(reference).split)
        caller_words = body(image, row.start, split.end(row), row.path)
        calls = [
            index
            for index, word in enumerate(caller_words)
            if word >> 26 in (2, 3) and _jump(word, caller_address + index * 4) == origin
        ]
        if not calls:
            continue
        caller = _named(project, version, Path(row.path).name, target_rows)
        if caller is None:
            continue
        target_row, at = caller
        if at + len(caller_words) * 4 > split.end(target_row) or not _equal(
            caller_words, _masks(caller_words), data, at
        ):
            continue
        address = split.address(target_row, project.version(version).split) + at - target_row.start
        for index in calls:
            destination = _jump(int.from_bytes(data[at + index * 4 : at + index * 4 + 4], "big"), address + index * 4)
            candidates = [span for span in candidates if span.address == destination]
        if len(candidates) <= 1:
            return candidates
    return candidates


def locate(project: Any, function: str, *, versions: Iterable[str] | None = None) -> dict[str, Span | None]:
    """Find one relocation-masked text span per VERSION; refuse ambiguous matches."""
    function = split.name(function)
    reference = _reference(project)
    rows = _inventory(project, reference)
    named = _named(project, reference, function, rows)
    if named is None:
        raise Held(
            cause_named(
                "layout.xver.locate",
                f"VERSION {reference} function {function}: missing reference placement",
                owner="layout.xver",
                stage="placement",
            )
        )
    row, start = named
    image = _image(project, reference)
    words = body(image, start, split.end(row), function)
    masks = _masks(words)
    fixed = [(index, word) for index, (word, mask) in enumerate(zip(words, masks, strict=False)) if not mask]
    if not fixed:
        raise Held(
            cause_named(
                "layout.xver.locate",
                f"function {function} relocation signature: no fixed instruction",
                owner="layout.xver",
                stage="placement",
            )
        )
    anchor_index, anchor = fixed[0]
    result: dict[str, Span | None] = {}
    selected = tuple(project.versions) if versions is None else tuple(dict.fromkeys((reference, *versions)))
    for version in selected:
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
            origin = split.address(row, project.version(reference).split) + start - row.start
            candidates = _disambiguate(project, function, reference, origin, words, image, version, data, candidates)
            if not candidates:
                raise Held(
                    cause_named(
                        "layout.xver.locate",
                        f"VERSION {version} function {function}: relocation/symbol evidence conflicts",
                        owner="layout.xver",
                        stage="placement",
                    )
                )
        if len(candidates) > 1:
            choices = ", ".join(f"0x{span.address:08X} (ROM 0x{span.start:X}, row {span.row})" for span in candidates)
            raise Held(
                cause_named(
                    "layout.xver.locate",
                    (
                        f"VERSION {version} function {function}: ambiguous relocation-masked "
                        f"placement; missing symbol {function} address selecting one of "
                        f"{len(candidates)} twins: {choices}"
                    ),
                    owner="layout.xver",
                    stage="placement",
                )
            )
        result[version] = candidates[0] if candidates else None
    return result


def _placement(project: Any, span: Span) -> list[PlacementNeed]:
    from unbake.decomp.needs import PlacementNeed

    evidence = f"VERSION {span.version} text at ROM 0x{span.start:X}..0x{span.end:X}"
    row = next(row for row in _inventory(project, span.version) if row.start <= span.start < split.end(row))
    result = []
    if row.start != span.start or split.end(row) != span.end:
        if row.kind != "asm":
            raise Held(
                cause_named(
                    "layout.xver._placement",
                    f"VERSION {span.version} function {span.function}: cannot cut c row {row.path}",
                    owner="layout.xver",
                    stage="placement",
                )
            )
        result.append(PlacementNeed(span.version, span.function, span.start, span.end, "cut", span.address, evidence))
    elif Path(row.path).name != span.function:
        if row.kind != "asm":
            raise Held(
                cause_named(
                    "layout.xver._placement",
                    f"VERSION {span.version} function {span.function}: cannot rename c row {row.path}",
                    owner="layout.xver",
                    stage="placement",
                )
            )
        result.append(
            PlacementNeed(span.version, span.function, span.start, span.end, "rename", Path(row.path).name, evidence)
        )
    symbol = _symbol(project, span.version, span.function)
    if symbol is None:
        result.append(PlacementNeed(span.version, span.function, span.start, span.end, "place", span.address, evidence))
    elif symbol[0] != span.address:
        raise Held(
            cause_named(
                "layout.xver._placement",
                f"VERSION {span.version} symbol {span.function}: conflicting address",
                owner="layout.xver",
                stage="placement",
            )
        )
    if span.align is not None:
        result.append(PlacementNeed(span.version, span.function, span.start, span.end, "align", span.align, evidence))
    return result


def needs(project: Any, function: str) -> list[PlacementNeed]:
    """Plan named function placement edits for match expansion."""
    return list(
        dict.fromkeys(
            need
            for span in locate(project, function).values()
            if span is not None
            for need in _placement(project, span)
        )
    )


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


register_resolver(PlacementNeed, 50, xver_edits.resolve)
