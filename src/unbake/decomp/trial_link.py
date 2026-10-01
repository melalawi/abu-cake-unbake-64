"""Inspect ELF objects and link a draft at its proved placement."""

from __future__ import annotations

import re
import shutil
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from unbake.decomp import needs as evidence
from unbake.decomp.trial_compile import run_tool
from unbake.decomp.trial_layout import NAME, FunctionSpan
from unbake.project.config import Held


@dataclass(frozen=True)
class Section:
    index: str
    name: str
    kind: str
    address: int
    offset: int
    size: int
    flags: str


@dataclass(frozen=True)
class Symbol:
    name: str
    address: int
    size: int
    kind: str
    binding: str
    section: str


@dataclass
class Elf:
    path: Path
    sections: dict[str, Section]
    symbols: list[Symbol]
    relocations: dict[str, dict[int, int]]


@dataclass(frozen=True)
class SectionPlacement:
    """Relocation-proved placement for scoring, without publication byte proof."""

    section: str
    address: int


def inspect(path: Path, readelf: str, work: Path) -> Elf:
    header = run_tool([readelf, "-hW", str(path)], work, "try")
    if (
        not re.search(r"Class:\s+ELF32", header)
        or not re.search(r"Data:.*big endian", header)
        or not re.search(r"Machine:.*MIPS", header, re.I)
    ):
        raise Held("try", f"{path}: ELF32 big-endian MIPS object is required")
    listing = run_tool([readelf, "-SW", "-sW", "-rW", str(path)], work, "try")
    sections: dict[str, Section] = {}
    symbols: list[Symbol] = []
    relocations: defaultdict[str, dict[int, int]] = defaultdict(dict)
    relocation_section = None
    for line in listing.splitlines():
        match = re.match(
            r"\s*\[\s*(\d+)\]\s+(\S+)\s+(\S+)\s+([\da-fA-F]+)\s+([\da-fA-F]+)\s+([\da-fA-F]+)\s+[\da-fA-F]+\s+(.*?)\s+\d+\s+\d+\s+\d+\s*$",
            line,
        )
        if match:
            index, name, kind, address, offset, size, flags = match.groups()
            sections[index] = Section(
                index, name, kind, int(address, 16), int(offset, 16), int(size, 16), flags.strip()
            )
            continue
        match = re.match(r"\s*\d+:\s+([\da-fA-F]+)\s+(\S+)\s+(\S+)\s+(\S+)\s+\S+\s+(\S+)\s+(.+?)\s*$", line)
        if match:
            address, size, kind, binding, section, name = match.groups()
            symbols.append(Symbol(name, int(address, 16), int(size, 0), kind, binding, section))
            continue
        match = re.match(r"Relocation section '(\.rela?)([^']+)'", line)
        if match:
            relocation_section = match[2]
            continue
        match = re.match(r"\s*([\da-fA-F]+)\s+[\da-fA-F]+\s+(R_MIPS_\w+)\b", line)
        if match and relocation_section is not None:
            offset, kind = match.groups()
            if kind == "R_MIPS_NONE":
                continue
            masks = {
                "R_MIPS_16": 0xFFFF,
                "R_MIPS_32": 0xFFFFFFFF,
                "R_MIPS_26": 0x03FFFFFF,
                "R_MIPS_HI16": 0xFFFF,
                "R_MIPS_LO16": 0xFFFF,
                "R_MIPS_GPREL16": 0xFFFF,
                "R_MIPS_PC16": 0xFFFF,
            }
            if kind not in masks:
                raise Held("try", f"{path}: unsupported relocation {kind} in {relocation_section}")
            relocations[relocation_section][int(offset, 16)] = masks[kind]
    if not sections or not symbols:
        raise Held("try", f"{path}: readelf sections/symbols are missing")
    return Elf(path, sections, symbols, dict(relocations))


def function_symbol(elf: Elf, function: str) -> Symbol:
    definitions = [symbol for symbol in elf.symbols if symbol.name == function and symbol.section in elf.sections]
    if len(definitions) != 1:
        raise Held("try", f"{elf.path}: one defined function symbol {function} is required")
    symbol = definitions[0]
    section = elf.sections[symbol.section]
    if "X" not in section.flags or not section.size:
        raise Held("try", f"{elf.path}: {function} has no executable body")
    return symbol


def generation_entry(layout: Elf, function: str, address: int) -> Symbol:
    """Prove the current generation has a function entry at the split address.

    Functions reached only through pointers carry the extractor's address-derived name until a
    draft names them, so the entry is identified by address and executable FUNC type.
    """
    named = [symbol for symbol in layout.symbols if symbol.name == function and symbol.section in layout.sections]
    if named:
        symbol = function_symbol(layout, function)
        if symbol.address != address:
            raise Held(
                "try",
                f"{layout.path}: {function} address 0x{symbol.address:X} "
                f"disagrees with symbol/split address 0x{address:X}",
            )
        return symbol
    entries = {
        symbol.name: symbol
        for symbol in layout.symbols
        if symbol.kind == "FUNC" and symbol.address == address and symbol.section in layout.sections
    }
    if len(entries) != 1:
        raise Held(
            "try", f"{layout.path}: one defined function symbol {function} or entry at 0x{address:X} is required"
        )
    return function_symbol(layout, next(iter(entries)))


def generation_elf(generation: Path) -> Path:
    paths = sorted(Path(generation).rglob("*.elf"))
    if len(paths) != 1:
        raise Held("try", f"{generation}: exactly one linked *.elf is required; found {len(paths)}")
    return paths[0]


def snapshot_layout(generation: Path, work: Path) -> Path:
    original = generation_elf(generation)
    snapshot = work / f"generation.{original.name}.snapshot"
    try:
        with original.open("rb") as source, snapshot.open("wb") as target:
            shutil.copyfileobj(source, target)
    except OSError as error:
        raise Held("try", f"{original}: cannot snapshot current generation: {error}") from error
    return snapshot


def link(
    unit: Elf,
    layout: Elf,
    function: str,
    span: FunctionSpan,
    values: dict[str, int],
    pending: Sequence[evidence.Need | SectionPlacement],
    linker: str,
    readelf: str,
    work: Path,
) -> tuple[bytes, dict[int, int], Path]:
    symbol = function_symbol(unit, function)
    section = unit.sections[symbol.section]
    if symbol.address != 0:
        raise Held("try", f"{unit.path}: {function} must start its text section; offset is 0x{symbol.address:X}")
    extra_functions = [
        s.name
        for s in unit.symbols
        if s.kind == "FUNC"
        and s.section in unit.sections
        and s.name != function
        and (s.section != symbol.section or s.address <= 0)
    ]
    if extra_functions:
        raise Held("try", f"{unit.path}: functions outside the draft split span: {', '.join(extra_functions)}")
    placements: dict[str, int] = {}
    for need in pending:
        if isinstance(need, SectionPlacement):
            if need.section in placements and placements[need.section] != need.address:
                raise Held("try", f"{need.section}: conflicting proved placements")
            placements[need.section] = need.address
        elif isinstance(need, evidence.RodataNeed):
            base = need.address - cast(dict[str, int], need.evidence)["offset"]
            if need.section in placements and placements[need.section] != base:
                raise Held("try", f"{need.section}: conflicting proved placements")
            placements[need.section] = base
    for other in unit.sections.values():
        if (
            "A" in other.flags
            and other.size
            and other.index != section.index
            and other.name not in (".reginfo", ".MIPS.abiflags")
            and other.name not in placements
        ):
            raise Held(
                "try",
                f"{unit.path}: placement for emitted section {other.name} "
                f"({other.size} bytes) is missing from current generation layout",
            )
    generation_entry(layout, function, span.address)
    addresses: dict[str, int] = {}
    for entry in layout.symbols:
        if entry.section != "UND" and entry.binding in ("GLOBAL", "WEAK"):
            if entry.name in addresses and addresses[entry.name] != entry.address:
                raise Held("try", f"{layout.path}: conflicting address for symbol {entry.name}")
            addresses[entry.name] = entry.address
    for name, address in values.items():
        if name in addresses and addresses[name] != address:
            raise Held("try", f"{layout.path}: {name} address disagrees with version.symbols")
        addresses[name] = address
    for need in pending:
        if isinstance(need, (evidence.SymbolNeed, evidence.LabelNeed)):
            if need.name in addresses and addresses[need.name] != need.address:
                raise Held("try", f"{need.name}: proved address disagrees with current generation layout")
            addresses[need.name] = need.address
    undefined = sorted({s.name for s in unit.symbols if s.section == "UND"})
    missing = [name for name in undefined if name not in addresses]
    if missing:
        raise Held("try", f"{unit.path}: symbol addresses missing: {', '.join(missing)}")
    for name in undefined:
        if not re.fullmatch(NAME, name):
            raise Held("try", f"{unit.path}: linker symbol name {name!r} is invalid")
    if not re.fullmatch(r"[\w.$]+", section.name):
        raise Held("try", f"{unit.path}: text section name {section.name!r} is invalid")
    script = work / "trial.ld"
    script.write_text(
        "\n".join(
            [f"{name} = 0x{addresses[name]:08X};" for name in undefined]
            + [
                f"SECTIONS {{ .text 0x{span.address:08X} : SUBALIGN({span.subalign}) {{ *({section.name}) }}",
                *(f"{name} 0x{address:08X} : SUBALIGN(1) {{ *({name}) }}" for name, address in placements.items()),
                "/DISCARD/ : { *(.reginfo) *(.MIPS.abiflags) *(.pdr) *(.mdebug*) *(.comment) *(.note*) } }",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    linked = work / "trial.elf"
    run_tool([linker, "-T", str(script), "-o", str(linked), str(unit.path)], work, "try")
    output = inspect(linked, readelf, work)
    placed = function_symbol(output, function)
    text = output.sections[placed.section]
    if placed.address != span.address or text.address != span.address:
        raise Held("try", f"{linked}: linker moved {function} away from 0x{span.address:X}")
    try:
        with linked.open("rb") as binary:
            binary.seek(text.offset)
            # A translation unit may contain later split functions. Keep all
            # residue in this span, stopping only at a defined function boundary.
            following = [
                entry.address - span.address
                for entry in output.symbols
                if entry.kind == "FUNC"
                and entry.section == placed.section
                and entry.address - span.address >= span.size
            ]
            size = min(text.size, min(following, default=text.size))
            if placed.size > text.size:
                raise Held("try", f"{linked}: {function} symbol size exceeds its text section")
            data = binary.read(size)
    except OSError as error:
        raise Held("try", f"{linked}: {error}") from error
    if len(data) != size or len(data) % 4:
        raise Held("try", f"{linked}: complete MIPS text words are missing")
    masks = {offset // 4: mask for offset, mask in unit.relocations.get(section.name, {}).items()}
    return data, masks, linked
