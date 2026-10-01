"""Explicit split metadata and ROM ranges for draft trials."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from unbake.decomp.trial_compile import read_text
from unbake.layout import split
from unbake.project.config import Held, Version

NAME = r"[A-Za-z_.$][\w.$]*"
NUMBER = r"(?:0[xX][0-9A-Fa-f]+|[0-9]+)"


def integer(value: str, where: str) -> int:
    value = value.strip()
    if not re.fullmatch(NUMBER, value):
        raise Held("try", f"{where}: explicit integer required, got {value!r}")
    return int(value, 16 if value.lower().startswith("0x") else 10)


@dataclass(frozen=True)
class FunctionSpan:
    address: int
    offset: int
    size: int
    subalign: int


def symbol_values(path: Path) -> dict[str, int]:
    values: dict[str, int] = {}
    for number, line in enumerate(read_text(path, "try").splitlines(), 1):
        line = line.split("//", 1)[0].split("#", 1)[0].strip()
        if not line:
            continue
        match = re.fullmatch(rf"({NAME})\s*=\s*({NUMBER})\s*;", line)
        if match is None:
            raise Held("try", f"{path}:{number}: symbol address NAME = INTEGER; required")
        name, value = match.groups()
        address = integer(value, f"{path}:{number} {name}")
        if name in values and values[name] != address:
            raise Held("try", f"{path}:{number}: conflicting symbol address {name}")
        values[name] = address
    return values


def function_span(version: Version, function: str, values: dict[str, int]) -> FunctionSpan | None:
    _, _, segments = split.layout(version.split)
    for segment in segments:
        named = [row for row in segment.rows if Path(row.path).stem == function]
        if not named:
            continue
        if len(named) != 1:
            raise Held("try", f"{version.split}: ambiguous split rows for {function}")
        for key in ("start", "vram", "subalign"):
            if key not in segment.fields:
                raise Held("try", f"{version.split}: segment for {function} is missing {key}")
        row = named[0]
        start = split.number(segment.fields["start"], f"{version.split}: segment start")
        vram = split.number(segment.fields["vram"], f"{version.split}: segment vram")
        address = values.get(function, split.address(row, version.split))
        if row.kind not in ("asm", "c"):
            raise Held("try", f"{version.split}: {function} row type {row.kind} is not asm or c")
        offset = start + address - vram
        if offset != row.start:
            raise Held(
                "try",
                f"{version.symbols}: {function} address 0x{address:X} disagrees with "
                f"{version.split} offset 0x{row.start:X}; cut a merged row first",
            )
        end = split.end(row)
        align = split.number(segment.fields["subalign"], f"{version.split}: subalign")
        if align <= 0 or align & (align - 1):
            raise Held("try", f"{version.split}: subalign {align} must be a positive power of two")
        if end <= offset or offset < start or (end - offset) % 4 or address % 4:
            raise Held("try", f"{version.split}: invalid word range for {function}: 0x{offset:X}..0x{end:X}")
        return FunctionSpan(address, offset, end - offset, align)
    if function not in values:
        return None
    raise Held("try", f"{version.split}: asm/c split row for symbol {function} is missing")


def target(version: Version, span: FunctionSpan) -> bytes:
    try:
        with Path(version.baserom).open("rb") as rom:
            rom.seek(span.offset)
            data = rom.read(span.size)
    except OSError as error:
        raise Held("try", f"{version.baserom}: {error}") from error
    if len(data) != span.size:
        raise Held("try", f"{version.baserom}: {span.size} bytes at ROM offset 0x{span.offset:X} are missing")
    return data


def rom_reader(version: Version) -> Callable[[int, int], bytes]:
    _, _, segments = split.layout(version.split)
    mappings = []
    for segment in segments:
        if "vram" not in segment.fields or "start" not in segment.fields or segment.fields.get("type") == "bss":
            continue
        end = segment.end
        if end is None:
            raise Held("try", f"{version.split}: mapped ROM end is missing")
        start = split.number(segment.fields["start"], f"{version.split}: segment start")
        vram = split.number(segment.fields["vram"], f"{version.split}: segment vram")
        bss = [row.start for row in segment.rows if row.kind.lstrip(".") == "bss"]
        if bss:
            end = min(end, min(bss))
        mappings.append((vram, vram + end - start, start))

    def read_memory(address: int, size: int) -> bytes:
        matches = [row for row in mappings if row[0] <= address and address + size <= row[1]]
        if size <= 0 or len(matches) != 1:
            raise Held("try", f"{version.name}.read_memory: unmapped or ambiguous range 0x{address:X}+{size}")
        start, _, offset = matches[0]
        return target(version, FunctionSpan(address, offset + address - start, size, 1))

    return read_memory
