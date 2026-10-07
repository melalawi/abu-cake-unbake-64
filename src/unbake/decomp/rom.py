"""Explicit split metadata and ROM ranges for draft trials."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path

from unbake.config import Held, Project, Version
from unbake.layout import split
from unbake.process import capture, read_text
from unbake.process import named as cause_named

NAME = r"[A-Za-z_.$][\w.$]*"
NUMBER = r"(?:0[xX][0-9A-Fa-f]+|[0-9]+)"


def integer(value: str, where: str) -> int:
    value = value.strip()
    if not re.fullmatch(NUMBER, value):
        raise Held(
            cause_named(
                f"{where}", f"{where}: explicit integer required, got {value!r}", owner="decomp.rom", stage="try"
            )
        )
    return int(value, 16 if value.lower().startswith("0x") else 10)


@dataclass(frozen=True)
class FunctionSpan:
    address: int
    offset: int
    size: int


def symbol_values(path: Path) -> dict[str, int]:
    values: dict[str, int] = {}
    for number, line in enumerate(read_text(path, "try").splitlines(), 1):
        line = line.split("//", 1)[0].split("#", 1)[0].strip()
        if not line:
            continue
        match = re.fullmatch(rf"({NAME})\s*=\s*({NUMBER})\s*;", line)
        if match is None:
            raise Held(
                cause_named(
                    f"{path}",
                    f"{path}:{number}: symbol address NAME = INTEGER; required",
                    owner="decomp.rom",
                    stage="try",
                )
            )
        name, value = match.groups()
        address = integer(value, f"{path}:{number} {name}")
        if name in values and values[name] != address:
            raise Held(
                cause_named(
                    f"{path}", f"{path}:{number}: conflicting symbol address {name}", owner="decomp.rom", stage="try"
                )
            )
        values[name] = address
    return values


def function_span(version: Version, function: str, values: dict[str, int]) -> FunctionSpan | None:
    _, _, segments = split.layout(version.split)
    for segment in segments:
        named = [row for row in segment.rows if Path(row.path).stem == function]
        if not named:
            continue
        if len(named) != 1:
            raise Held(
                cause_named(
                    f"{version.split}",
                    f"{version.split}: ambiguous split rows for {function}",
                    owner="decomp.rom",
                    stage="try",
                )
            )
        for key in ("start", "vram"):
            if key not in segment.fields:
                raise Held(
                    cause_named(
                        f"{version.split}",
                        f"{version.split}: segment for {function} is missing {key}",
                        owner="decomp.rom",
                        stage="try",
                    )
                )
        row = named[0]
        start = split.number(segment.fields["start"], f"{version.split}: segment start")
        vram = split.number(segment.fields["vram"], f"{version.split}: segment vram")
        address = values.get(function, split.address(row, version.split))
        if row.kind not in ("asm", "c"):
            raise Held(
                cause_named(
                    f"{version.split}",
                    f"{version.split}: {function} row type {row.kind} is not asm or c",
                    owner="decomp.rom",
                    stage="try",
                )
            )
        offset = start + address - vram
        if offset != row.start:
            raise Held(
                cause_named(
                    f"{version.symbols}",
                    (
                        f"{version.symbols}: {function} address 0x{address:X} disagrees "
                        f"with {version.split} offset 0x{row.start:X}; cut a merged row first"
                    ),
                    owner="decomp.rom",
                    stage="try",
                )
            )
        end = split.end(row)
        if end <= offset or offset < start or (end - offset) % 4 or address % 4:
            raise Held(
                cause_named(
                    f"{version.split}",
                    f"{version.split}: invalid word range for {function}: 0x{offset:X}..0x{end:X}",
                    owner="decomp.rom",
                    stage="try",
                )
            )
        return FunctionSpan(address, offset, end - offset)
    if function not in values:
        return None
    raise Held(
        cause_named(
            f"{version.split}",
            f"{version.split}: asm/c split row for symbol {function} is missing",
            owner="decomp.rom",
            stage="try",
        )
    )


def target(version: Version, span: FunctionSpan) -> bytes:
    try:
        with Path(version.baserom).open("rb") as rom:
            rom.seek(span.offset)
            data = rom.read(span.size)
    except OSError as error:
        raise Held(
            capture(
                error,
                cause=cause_named(f"{version.baserom}", f"{version.baserom}: {error}", owner="decomp.rom", stage="try"),
            )
        ) from error
    if len(data) != span.size:
        raise Held(
            cause_named(
                f"{version.baserom}",
                f"{version.baserom}: {span.size} bytes at ROM offset 0x{span.offset:X} are missing",
                owner="decomp.rom",
                stage="try",
            )
        )
    return data


@dataclass(frozen=True)
class MemorySpan:
    address: int
    end: int
    offset: int
    table_entry_bias: int


class RomReader:
    """Resolve one ROM span for both raw constants and biased table pointers."""

    def __init__(self, version: Version, resident: Callable[[], list[dict[str, int]]]) -> None:
        self.version = version
        self.resident = resident
        self.copies: list[MemorySpan] | None = None
        self.mappings: list[MemorySpan] = []
        _, _, self.segments = split.layout(version.split)
        for segment in self.segments:
            if "vram" not in segment.fields or "start" not in segment.fields or segment.fields.get("type") == "bss":
                continue
            end = segment.end
            if end is None:
                raise Held(
                    cause_named(
                        f"{version.split}",
                        f"{version.split}: mapped ROM end is missing",
                        owner="decomp.rom",
                        stage="try",
                    )
                )
            start = split.number(segment.fields["start"], f"{version.split}: segment start")
            vram = split.number(segment.fields["vram"], f"{version.split}: segment vram")
            bss = [row.start for row in segment.rows if row.kind.lstrip(".") == "bss"]
            if bss:
                end = min(end, min(bss))
            self.mappings.append(MemorySpan(vram, vram + end - start, start, 0))

    def resident_spans(self) -> list[MemorySpan]:
        """Load configured copies once, including pointer identity after migration."""
        if self.copies is None:
            self.copies = [
                MemorySpan(
                    row["address"], row["address"] + row["end"] - row["start"], row["start"], row["table_entry_bias"]
                )
                for row in self.resident()
            ]
        return self.copies

    def find_span(self, address: int, size: int) -> MemorySpan | None:
        """Find ROM backing, distinguishing absent bytes from ambiguous mappings."""
        matches = [row for row in self.mappings if row.address <= address and address + size <= row.end]
        if not matches:
            matches = [row for row in self.resident_spans() if row.address <= address and address + size <= row.end]
        if size <= 0 or len(matches) > 1:
            raise Held(
                cause_named(
                    f"{self.version.name}.read_memory",
                    f"{self.version.name}.read_memory: unmapped or ambiguous range 0x{address:X}+{size}",
                    owner="decomp.rom",
                    stage="try",
                )
            )
        return matches[0] if matches else None

    def span(self, address: int, size: int) -> MemorySpan:
        mapping = self.find_span(address, size)
        if mapping is None:
            raise Held(
                cause_named(
                    f"{self.version.name}.read_memory",
                    f"{self.version.name}.read_memory: unmapped or ambiguous range 0x{address:X}+{size}",
                    owner="decomp.rom",
                    stage="try",
                )
            )
        return mapping

    def backing_row(self, address: int, size: int) -> tuple[int, split.Row]:
        """Resolve a runtime span to its unique ROM-backed data split row."""
        mapping = self.span(address, size)
        offset = mapping.offset + address - mapping.address
        rows = [
            row
            for segment in self.segments
            for row in segment.rows
            if row.kind.lstrip(".") in ("data", "rodata", "rdata", "bin")
            and row.start <= offset
            and offset + size <= split.end(row)
        ]
        if len(rows) != 1:
            raise Held(
                cause_named(
                    f"0x{address:08X}+{size}",
                    f"0x{address:08X}+{size}: resident row is missing or ambiguous",
                    owner="decomp.rom",
                    stage="rodata",
                )
            )
        return offset, rows[0]

    def __call__(self, address: int, size: int) -> bytes:
        mapping = self.span(address, size)
        return target(self.version, FunctionSpan(address, mapping.offset + address - mapping.address, size))

    def table_span(self, address: int, size: int) -> MemorySpan:
        """The unique backing and configured pointer identity of a complete table range."""
        mapping = self.span(address, size)
        # Migration can give a resident copy its own native split segment. The
        # raw bytes then have a direct mapping, but their configured pointer bias
        # still describes the same runtime table identity.
        overrides = [row for row in self.resident_spans() if row.address < address + size and address < row.end]
        if len(overrides) > 1 or any(
            not row.address <= address < address + size <= row.end
            or row.offset + address - row.address != mapping.offset + address - mapping.address
            for row in overrides
        ):
            raise Held(
                cause_named(
                    f"{self.version.name}.table_entry",
                    f"{self.version.name}.table_entry: conflicting resident backing at 0x{address:X}",
                    owner="decomp.rom",
                    stage="try",
                )
            )
        return replace(mapping, table_entry_bias=overrides[0].table_entry_bias) if overrides else mapping

    def table_entry(self, address: int) -> int:
        mapping = self.table_span(address, 4)
        return (int.from_bytes(self(address, 4), "big") + mapping.table_entry_bias) & 0xFFFFFFFF


def rom_reader(version: Version, resident: Callable[[], list[dict[str, int]]]) -> RomReader:
    """Map split segments, then explicit resident runtime copies of ROM spans."""
    return RomReader(version, resident)


def project_reader(project: Project, name: str) -> RomReader:
    """Read a VERSION's memory, including its configured resident copies of ROM spans."""

    return rom_reader(
        project.version(name),
        lambda: [dict(vars(row)) for row in project.resident_mappings.get(name, ())],
    )
