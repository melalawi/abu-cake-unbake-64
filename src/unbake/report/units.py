"""Discover report units and their target and partial objects."""

from __future__ import annotations

import hashlib
import os
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from unbake.layout import split
from unbake.project.config import Held, Policy, Project, Version
from unbake.project_tools.elf import Object, Symbol
from unbake.project_tools.extract import partial_rows
from unbake.report import files


@dataclass(frozen=True)
class Function:
    name: str
    path: str
    kind: str
    start: int
    end: int
    address: int | None


def functions(version: Version) -> list[Function]:
    """Count every text row, including startup and handwritten assembly."""
    _, _, segments = split.layout(version.split)
    return [
        Function(
            Path(row.path).name,
            row.path,
            row.kind,
            row.start,
            segment.rows[index + 1].start
            if index + 1 < len(segment.rows)
            else split.number(segment.end, "segment end"),
            split.address(row, version.split) if row.kind == "c" else None,
        )
        for segment in segments
        for index, row in enumerate(segment.rows)
        if row.kind in ("asm", "hasm", "c")
    ]


class LinkedImage:
    """Index definitions and executable section bytes once per linked image."""

    def __init__(self, obj: Object) -> None:
        self.object = obj
        self.symbols: dict[str, list[Symbol]] = {}
        for table in obj.symbols.values():
            for symbol in table:
                if 0 < symbol["section"] < len(obj.sections):
                    self.symbols.setdefault(symbol["name"], []).append(symbol)
        self.code = {index: obj.content(index) for index, section in enumerate(obj.sections) if section[2] & 4}


def read_object(path: Path) -> Object:
    try:
        return Object(path)
    except (OSError, ValueError, IndexError, struct.error) as error:
        raise Held("report", f"object {path}: {error}") from error


def linked_code(base: Path, linked: LinkedImage | None, row: Function) -> bytes:
    """Read resolved C instructions at their verified split address."""
    size = row.end - row.start
    if linked is None:
        obj = read_object(base)
        text = obj.section(".text")
        if text is None or obj.relocations(text):
            raise Held("report", f"matched function {row.name}: linked ELF is missing")
        code = obj.content(text)
        if len(code) < size or any(code[size:]):
            raise Held("report", f"matched function {row.name}: object text disagrees with split range")
        return code[:size]
    if row.address is None:
        raise Held("report", f"matched function {row.name}: split address is missing")
    symbols = linked.symbols.get(row.name, [])
    if len(symbols) != 1:
        raise Held("report", f"matched function {row.name}: one linked definition is required")
    symbol = symbols[0]
    section = linked.object.sections[symbol["section"]]
    offset = symbol["value"] - section[3]
    if symbol["value"] != row.address or not section[2] & 4 or offset < 0 or offset + size > section[5]:
        raise Held("report", f"matched function {row.name}: linked address or text range disagrees with split")
    return linked.code[symbol["section"]][offset : offset + size]


def full_text_object(path: Path, size: int) -> bool:
    """Keep assembly targets only when their symbols cover the full split interval."""
    obj = read_object(path)
    text = obj.section(".text")
    if text is None or len(obj.content(text)) < size or any(obj.content(text)[size:]):
        raise Held("report", f"target assembly object {path}: text size disagrees with split")
    spans = sorted(
        (symbol["value"], symbol["value"] + symbol["size"])
        for table in obj.symbols.values()
        for symbol in table
        if symbol["section"] == text and symbol["info"] & 15 == 2 and symbol["size"]
    )
    end = 0
    for start, stop in spans:
        if start != end:
            return False
        end = stop
    return end == size


def units(project: Project, policy: Policy, version: str, generation: Path, workspace: Path) -> list[dict[str, Any]]:
    from unbake.report.progress import target_object

    cartridge = project.version(version)
    try:
        image = cartridge.baserom.read_bytes()
    except OSError as error:
        raise Held("report", f"VERSION {version} baserom {cartridge.baserom}: {error}") from error
    found = functions(cartridge)
    if not found:
        raise Held("report", f"VERSION {version} split {cartridge.split}: functions are missing")
    try:
        _, _, partial_segments = split.parse_layout(
            cartridge.split, partial_rows(split.read(cartridge.split), project.src)
        )
    except (OSError, UnicodeError, ValueError) as error:
        raise Held("report", f"VERSION {version} partial sources: {error}") from error
    partial_paths = {row.path for segment in partial_segments for row in segment.rows if row.kind == "c"}
    linked_paths = sorted(generation.glob("*.elf"))
    if len(linked_paths) > 1:
        raise Held("report", f"VERSION {version}: exactly one linked ELF is required")
    linked = LinkedImage(read_object(linked_paths[0])) if linked_paths else None
    units = []
    names = set()
    for row in found:
        function = row.name
        if function in names:
            raise Held("report", f"VERSION {version} duplicate function {function}")
        names.add(function)
        if row.start < 0 or row.end <= row.start or row.end > len(image):
            raise Held("report", f"VERSION {version} function {function} ROM range is invalid")
        target_bytes = target_object(function, image[row.start : row.end])
        target = workspace / "target" / (hashlib.sha256(target_bytes).hexdigest() + ".o")
        if row.kind in ("asm", "hasm"):
            assembly = generation / "obj" / "asm" / (row.path + ".o")
            if not assembly.is_file():
                raise Held("report", f"VERSION {version} target assembly object {assembly} is missing")
            if full_text_object(assembly, row.end - row.start):
                target = assembly
        if not target.exists():
            files.write(target, target_bytes)
        unit: dict[str, Any] = {
            "name": function,
            "target_path": os.path.relpath(target, generation),
            "metadata": {"complete": row.kind == "c"},
        }
        if row.kind == "c":
            source = project.src / (row.path + ".c")
            base = generation / "obj" / "src" / (row.path + ".o")
            if not source.is_file():
                raise Held("report", f"matched function {function} source {source} is missing")
            if not base.is_file():
                raise Held("report", f"matched function {function} linked src object {base} is missing")
            resolved = target_object(function, linked_code(base, linked, row))
            base = workspace / "linked" / (hashlib.sha256(resolved).hexdigest() + ".o")
            if not base.exists():
                files.write(base, resolved)
        else:
            source = project.src / (row.path + ".c")
            base = None
            if row.kind == "asm" and row.path in partial_paths:
                base = project.root / "build" / (version + ".nonmatching") / "obj" / "src" / (row.path + ".o")
                if not base.is_file():
                    raise Held(
                        "report",
                        f"draft {source} partial src object {base} is missing; "
                        f"run make -j4 VERSION={version} NON_MATCHING=1 "
                        f"{base.relative_to(project.root)}",
                    )
        if base is not None:
            unit["base_path"] = os.path.relpath(base, generation)
            unit["metadata"]["source_path"] = os.path.relpath(source, project.root)
        units.append(unit)
    return units
