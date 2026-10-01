"""Final placements for relocatable trial operands and resident jump tables."""

from __future__ import annotations

import re
import shutil
import struct
from pathlib import Path
from typing import Any

from unbake.project_tools.elf import Object


def paired_relocation_addresses(
    candidate: Path, sections: dict[str, int], addresses: dict[str, int]
) -> dict[int, tuple[int, int]]:
    """Resolve ELF HI16/LO16 pairs, including objdiff's synthetic pool symbols.

    Objdiff names inferred pool slices but omits their containing section. ELF
    relocation records retain that identity; only a proved section base is used.
    """
    from unbake.project_tools.literal_layout import signed

    obj = Object(candidate)
    text = obj.section(".text")
    if text is None:
        return {}
    code = obj.content(text)
    pending: dict[tuple[str, int, int], list[tuple[int, int]]] = {}
    result = {}
    for offset, kind, symbol in obj.relocations(text):
        if kind not in (5, 6):
            continue
        key = symbol["name"], symbol["value"], symbol["section"]
        word = int.from_bytes(code[offset : offset + 4], "big")
        if kind == 5:
            pending.setdefault(key, []).append((offset, word))
            continue
        highs = pending.pop(key, [])
        if symbol["section"] == 0:
            base = addresses.get(symbol["name"])
        elif symbol["section"] == 0xFFF1:
            base = symbol["value"]
        elif symbol["section"] < len(obj.names):
            section_base = sections.get(f"[{obj.names[symbol['section']]}]")
            base = None if section_base is None else section_base + symbol["value"]
        else:
            base = None
        if base is None:
            continue
        lows = set()
        for at, high in highs:
            address = (base + ((high & 0xFFFF) << 16) + signed(word)) & 0xFFFFFFFF
            result[at] = 5, address
            lows.add(address)
        if len(lows) == 1:
            result[offset] = 6, lows.pop()
    return result


def resolve_literal_placement(
    generation: Path,
    version: str,
    function: str,
    candidate: Path,
    output: Path,
    addresses: dict[str, int],
) -> tuple[Path, dict[str, int]]:
    """Byte-prove native pools on a private copy using the build layout boundary.

    arrange owns pairing, literal duplication, table identity and resident gaps.
    A failed proof leaves the compiler object and its comparison untouched.
    """
    from unbake.decomp.rom import project_reader
    from unbake.layout import split
    from unbake.project.config import Held, load
    from unbake.project_tools.literal_layout import arrange

    root = generation.parent.parent
    if not (root / "config.toml").is_file():
        return candidate, {}
    obj = Object(candidate)
    text = obj.section(".text")
    entries = [s for table in obj.symbols.values() for s in table if s["name"] == function and s["section"] == text]
    if text is None or len(entries) != 1 or function not in addresses:
        return candidate, {}
    pools = [
        section
        for section in (".rdata", ".rodata")
        if (index := obj.section(section)) is not None
        and obj.sections[index][5]
        and any(symbol["section"] == index for _, _, symbol in obj.relocations(text))
    ]
    if not pools:
        return candidate, {}
    project = load(root)
    owners = [row for row in split.functions(project, version) if function in row.aliases]
    if len(owners) != 1:
        return candidate, {}
    owner = owners[0]
    text_base = addresses[function] - entries[0]["value"]
    # Only owning ROM words may guide layout; extra candidate instructions are
    # code differences and cannot extend the placement evidence into a neighbour.
    resident = split.words(project, owner)
    targets = {
        entries[0]["value"] + at: int.from_bytes(resident[at : at + 4], "big") for at in range(0, len(resident), 4)
    }
    reader = project_reader(project, version)
    resolved: dict[str, int] = {}
    copied = output.with_suffix(".placed.o")
    for section in pools:
        if not resolved:
            shutil.copyfile(candidate, copied)
        try:
            base = arrange(
                Object(copied),
                section,
                targets,
                text_base,
                reader,
                lambda address, size: b"".join(
                    reader.table_entry(address + at).to_bytes(4, "big") for at in range(0, size, 4)
                ),
            )
        except (Held, ValueError, KeyError, struct.error):
            continue
        resolved[f"[{section}]"] = base
    if not resolved:
        copied.unlink(missing_ok=True)
        return candidate, {}
    return copied, {"[.text]": text_base, **resolved}


def placements(generation: Path, function: str, target: Path) -> dict[str, dict[str, int]]:
    """Read object-qualified input sections; never infer a section base from an operand."""
    result: dict[str, dict[str, int]] = {"left": {}, "right": {}}
    try:
        owner = target.resolve().relative_to(generation.resolve()).as_posix()
    except ValueError:
        owner = ""
    for path in sorted(generation.glob("*.map")):
        text = path.read_text()
        for match in re.finditer(r"^\s+(\.[\w.]+)\s+(0x[\da-fA-F]+)\s+0x[\da-fA-F]+\s+(obj/[^\s]+\.o)\s*$", text, re.M):
            section, address, unit = match.groups()
            if unit == owner:
                result["left"][f"[{section}]"] = int(address, 16)
            if unit == f"obj/src/{function}.o":
                result["right"][f"[{section}]"] = int(address, 16)
    return result


def jump_table_differences(
    generation: Path,
    version: str,
    candidate: Path,
    sections: dict[str, int],
    addresses: dict[str, int],
    *,
    complete_table: bool = False,
    section_name: str = ".rdata",
) -> list[str]:
    """Compare R_MIPS_32 table entries with linked resident bytes when placement is known."""
    obj = Object(candidate)
    index = obj.section(section_name)
    base = sections.get(f"[{section_name}]")
    if index is None:
        return []
    entries = {offset: symbol for offset, kind, symbol in obj.relocations(index) if kind == 2}
    if base is None:
        return ["jump table: draft placement unresolved"] if entries else []
    linked_paths = sorted(generation.glob("*.elf"))
    linked = Object(linked_paths[0]) if len(linked_paths) == 1 else None
    from unbake.decomp.rom import project_reader
    from unbake.project.config import load

    reader = (
        project_reader(load(generation.parent.parent), version)
        if (generation.parent.parent / "config.toml").is_file()
        else None
    )
    data = obj.content(index)
    differences = []

    def expected_at(location: int, pointer: bool) -> int | None:
        if reader is not None and reader.find_span(location, 4) is not None:
            return reader.table_entry(location) if pointer else int.from_bytes(reader(location, 4), "big")
        if linked is None:
            return None
        loaded = [
            i
            for i, section in enumerate(linked.sections)
            if section[3] <= location and location + 4 <= section[3] + section[5]
        ]
        if len(loaded) != 1:
            return None
        section = loaded[0]
        return int(struct.unpack_from(">I", linked.content(section), location - linked.sections[section][3])[0])

    for offset in range(0, len(data) - 3, 4):
        symbol = entries.get(offset)
        addend = struct.unpack_from(">I", data, offset)[0]
        destination: int | None = 0
        name = "" if symbol is None else symbol["name"]
        if symbol is None:
            pass
        elif symbol["section"] == 0:
            destination = addresses.get(name)
        elif symbol["section"] == 0xFFF1:
            destination = symbol["value"]
        elif symbol["section"] < len(obj.names):
            section_base = sections.get(f"[{obj.names[symbol['section']]}]")
            destination = None if section_base is None else section_base + symbol["value"]
        else:
            destination = None
        if destination is None:
            differences.append(f"jump table +0x{offset:X}: unresolved target {name!r}")
            continue
        destination = (destination + addend) & 0xFFFFFFFF
        location = base + offset
        expected = expected_at(location, symbol is not None)
        if expected is None:
            differences.append(f"jump table +0x{offset:X}: linked target bytes unavailable")
            continue
        if destination != expected:
            differences.append(f"jump table +0x{offset:X}: target 0x{expected:08X}; draft 0x{destination:08X}")
    if complete_table and len(data) - 4 in entries:
        following = expected_at(base + len(data), True)
        text = obj.section(".text")
        text_base = sections.get("[.text]")
        if text is not None and text_base is not None and following is not None:
            functions = [
                symbol
                for table in obj.symbols.values()
                for symbol in table
                if symbol["section"] == text and symbol["info"] & 15 == 2
            ]
            if any(text_base + s["value"] <= following < text_base + s["value"] + s["size"] for s in functions):
                differences.append(f"jump table +0x{len(data):X}: resident table has a missing draft entry")
    return differences


def resolve_table_placement(
    document: dict[str, Any],
    function: str,
    candidate: Path,
    sections: dict[str, dict[str, int]],
    addresses: dict[str, int],
) -> bool:
    """Resolve a draft-owned table through all its aligned resident table uses.

    This is a table placement proof, not a general section-base guess: the entire
    section must consist of text-pointer relocations, start at the first use,
    and every HI/LO use must agree on one placement. Its entries are subsequently
    checked against resident bytes by jump_table_differences.
    """
    obj = Object(candidate)
    text = obj.section(".text")
    data = obj.section(".rdata")
    if text is None or data is None or "[.rdata]" in sections["right"]:
        return False
    entries = obj.relocations(data)
    size = len(obj.content(data))
    if not size or {offset for offset, _, _ in entries} != set(range(0, size, 4)):
        return False
    if any(kind != 2 or symbol["section"] != text for _, kind, symbol in entries):
        return False
    left, right = document["left"], document["right"]
    target = next(s for s in left["symbols"] if s.get("name") == function and s.get("kind") == "SYMBOL_FUNCTION")
    draft = next(s for s in right["symbols"] if s.get("name") == function and s.get("kind") == "SYMBOL_FUNCTION")
    target_rows = {
        int(row["instruction"].get("address", 0)) - int(target.get("address", 0)): row["instruction"]
        for row in target.get("instructions", [])
        if "instruction" in row
    }
    uses: dict[int, set[int]] = {}
    bases = set()
    for row in draft.get("instructions", []):
        instruction = row.get("instruction", {})
        relocation = instruction.get("relocation")
        if not relocation:
            continue
        symbol = right["symbols"][int(relocation["target_symbol"])]
        if symbol["name"] != "[.rdata]":
            continue
        kind = int(relocation.get("type", 0))
        offset = int(relocation.get("addend", 0))
        before = target_rows.get(int(instruction.get("address", 0)) - int(draft.get("address", 0)), {})
        reference = before.get("relocation", {})
        if kind not in (5, 6) or int(reference.get("type", 0)) != kind or not 0 <= offset < size or offset % 4:
            return False
        name = left["symbols"][int(reference["target_symbol"])]["name"]
        address = sections["left"].get(name, addresses.get(name))
        if address is None or int(reference.get("addend", 0)) != 0:
            return False
        bases.add(address + int(reference.get("addend", 0)) - offset)
        uses.setdefault(offset, set()).add(kind)
    if len(bases) != 1 or not uses or min(uses) != 0 or any(kinds != {5, 6} for kinds in uses.values()):
        return False
    # Text pointers are resolved at the same function placement, never from a
    # table entry's numeric spelling. An explicit map placement takes precedence.
    address = addresses.get(function)
    symbols = [s for table in obj.symbols.values() for s in table if s["name"] == function and s["section"] == text]
    if address is None or len(symbols) != 1:
        return False
    sections["right"].setdefault("[.text]", address - symbols[0]["value"])
    sections["right"]["[.rdata]"] = bases.pop()
    return True
