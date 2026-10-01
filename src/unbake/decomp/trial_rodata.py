"""Separate trial section placement and differences from publication proof."""

from collections.abc import Callable
from pathlib import Path
from typing import Any

from unbake.decomp.trial_artifacts import Artifact
from unbake.decomp.trial_compare import Compare, words
from unbake.decomp.trial_layout import RomReader, project_reader
from unbake.decomp.trial_link import SectionPlacement, inspect
from unbake.project.config import Held, Project
from unbake.project_tools.rodata import placement


def rodata_reader(reader: RomReader, obj: Any, bases: dict[str, int]) -> Callable[[int, int], bytes]:
    """Normalize only relocated text pointers; preserve literal and padding bytes."""
    pointers = []
    text = obj.section(".text")
    for section, base in bases.items():
        for offset, kind, symbol in obj.relocations(obj.section(section)):
            if kind == 2 and symbol["section"] == text:
                pointers.append(base + offset)

    def read_memory(address: int, size: int) -> bytes:
        data = bytearray(reader(address, size))
        for pointer in pointers:
            if address <= pointer and pointer + 4 <= address + size:
                offset = pointer - address
                data[offset : offset + 4] = reader.table_entry(pointer).to_bytes(4, "big")
        return bytes(data)

    return read_memory


def section_placements(project: Project, artifact: Artifact, function: str) -> list[SectionPlacement]:
    constants = artifact["rodata"]
    pools = constants.family.jump_tables(constants.obj) + constants.family.literal_pools(constants.obj)
    result: dict[str, SectionPlacement] = {}
    for pool in pools:
        if function not in constants.owners[(pool.section, pool.offset)]:
            raise Held("rodata", f"owners[{pool.section},{pool.offset}]: excludes {function}")
        if pool.section not in result:
            base, _ = placement(constants.obj, pool.section, constants.target_words)
            constants.read_memory(base, len(constants.obj.content(constants.obj.section(pool.section))))
            result[pool.section] = SectionPlacement(pool.section, base)
    return list(result.values())


def pool_guidance(project: Project, artifact: Artifact) -> list[str]:
    constants = artifact["rodata"]
    tables = constants.family.jump_tables(constants.obj)
    result = []
    reader = project_reader(project, artifact["version"].name)
    for table in tables:
        base, _ = placement(constants.obj, table.section, constants.target_words)
        address = base + table.offset
        offset, row = reader.backing_row(address, table.size)
        result.append(
            f"jump table: {constants.function} owns {table.section} table 0x{address:08X} (jtbl_{address:08X}); "
            f"ROM offset 0x{offset:X}; size 0x{table.size:X}; resident {row.kind} {row.path}"
        )
    return result


def compare_rodata(artifact: Artifact, linked: Path, readelf: str, comparison: Compare) -> None:
    constants = artifact["rodata"]
    output = inspect(linked, readelf, artifact["work"])
    binary = linked.read_bytes()
    differences = 0
    for placed in artifact["placements"]:
        matches = [section for section in output.sections.values() if section.name == placed.section]
        if len(matches) != 1:
            raise Held("try", f"{placed.section}: linked section is missing or ambiguous")
        section = matches[0]
        actual = binary[section.offset : section.offset + section.size]
        expected = constants.read_memory(placed.address, section.size)
        if len(actual) != len(expected) or len(actual) % 4:
            raise Held("try", f"{placed.section}: complete rodata words are missing")
        for index, (target, draft) in enumerate(zip(words(expected), words(actual), strict=True)):
            if target != draft:
                differences += 1
                comparison.lines.append(
                    f"rodata {placed.section}+0x{index * 4:X}: target {target:08X}; draft {draft:08X}"
                )
    if differences:
        comparison.typed["rodata"] = differences
