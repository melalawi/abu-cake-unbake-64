"""Separate trial section placement and differences from publication proof."""

from pathlib import Path

from unbake.decomp.trial_artifacts import Artifact
from unbake.decomp.trial_compare import Compare, words
from unbake.decomp.trial_link import SectionPlacement, inspect
from unbake.layout import split
from unbake.project.config import Held, Project
from unbake.project_tools.rodata import placement


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
    _, _, segments = split.layout(artifact["version"].split)
    tables = constants.family.jump_tables(constants.obj)
    result = []
    for table in tables:
        base, _ = placement(constants.obj, table.section, constants.target_words)
        address = base + table.offset
        rows = [
            row
            for segment in segments
            for row in segment.rows
            if row.kind.lstrip(".") in ("data", "rodata", "rdata")
            and split.address(row, artifact["version"].split) <= address
            and address + table.size <= split.address(row, artifact["version"].split) + split.end(row) - row.start
        ]
        if len(rows) != 1:
            raise Held("rodata", f"{table.section}: table resident row is missing or ambiguous")
        row = rows[0]
        offset = row.start + address - split.address(row, artifact["version"].split)
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
