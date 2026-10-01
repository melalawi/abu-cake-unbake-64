"""Trial context and constant-pool evidence from compilation artifacts."""

from __future__ import annotations

from contextlib import suppress
from dataclasses import dataclass
from difflib import SequenceMatcher
from itertools import pairwise
from pathlib import Path
from typing import TYPE_CHECKING, NotRequired, TypedDict

from unbake.decomp.trial_compare import fields, words
from unbake.decomp.trial_layout import FunctionSpan, project_reader
from unbake.decomp.trial_link import Elf, SectionPlacement, function_range
from unbake.layout.rodata import TrialObject
from unbake.project.config import Held, Policy, Project, Version

if TYPE_CHECKING:
    from unbake.decomp.trial import Trial


class Artifact(TypedDict):
    unit: Elf
    layout: Elf
    target_words: list[int]
    span: FunctionSpan
    version: Version
    work: Path
    rodata: NotRequired[TrialObject]
    placements: NotRequired[list[SectionPlacement]]
    private_rodata: NotRequired[set[str]]


@dataclass(frozen=True)
class TrialContext:
    project: Project
    policy: Policy
    source: Path
    trial: Trial
    artifacts: dict[str, Artifact]


def rodata_object(
    project: Project, source: Path, function: str, artifact: Artifact, values: dict[str, int]
) -> TrialObject:
    from unbake.decomp.indexed import indexed_references
    from unbake.decomp.symbols import references
    from unbake.decomp.trial_rodata import private_address, rodata_reader
    from unbake.families import family_for
    from unbake.layout import split
    from unbake.layout.rodata import TrialObject
    from unbake.project_tools.elf import Object
    from unbake.project_tools.literal_layout import arrange
    from unbake.project_tools.rodata import placement, relocated

    obj = Object(artifact["unit"].path)
    family = family_for(project.compiler_for(source).id)
    entry, section, end = function_range(artifact["unit"], function, artifact["span"].size)
    text = obj.section(section.name)
    if text is None:
        raise Held("try", f"{obj.path}: .text is missing")
    candidate = words(obj.content(text)[entry.address : end])
    text_base = artifact["span"].address - entry.address
    target = artifact["target_words"]

    # Keep register/opcode evidence while aligning unresolved immediate operands.
    def shape(word: int) -> int:
        return word & ~fields(word)[1]

    aligned: dict[int, int | None] = {offset: None for offset in range(0, len(obj.content(text)), 4)}
    matcher = SequenceMatcher(
        None, [shape(word) for word in target], [shape(word) for word in candidate], autojunk=False
    )
    for block in matcher.get_matching_blocks():
        for delta in range(block.size):
            aligned[entry.address + (block.b + delta) * 4] = target[block.a + delta]
    read_memory = project_reader(project, artifact["version"].name)

    def read_table(address: int, size: int) -> bytes:
        return b"".join(read_memory.table_entry(address + at).to_bytes(4, "big") for at in range(0, size, 4))

    pools = family.literal_pools(obj) + family.jump_tables(obj)
    artifact["private_rodata"] = set()
    bases = {}
    for pool in pools:
        if pool.section not in bases:
            try:
                try:
                    base = placement(obj, pool.section, aligned)[0]
                except ValueError as initial:
                    try:
                        base = arrange(obj, pool.section, aligned, text_base, read_memory, read_table)
                    except ValueError:
                        raise initial from None
                else:
                    section_index = obj.section(pool.section)
                    assert section_index is not None
                    if read_memory(base, len(obj.content(section_index))) != relocated(obj, pool.section, text_base):
                        # Imperfect pools retain their ordinary scoring placement.
                        with suppress(ValueError):
                            base = arrange(obj, pool.section, aligned, text_base, read_memory, read_table)
                bases[pool.section] = base
            except (ValueError, Held):
                bases[pool.section] = private_address(artifact, pool.section)
                artifact["private_rodata"].add(pool.section)
    pools = family.literal_pools(obj) + family.jump_tables(obj)
    owners: dict[tuple[str, int], set[str]] = {(pool.section, pool.offset): set() for pool in pools}
    if pools:
        for symbol in split.functions(project, artifact["version"].name):
            body = words(read_memory(symbol.address, symbol.end - symbol.start))
            addresses = {ref.address for ref in references(body, values.get("_gp"))}
            addresses.update(ref.address for ref in indexed_references(body))
            for high, low in pairwise(body):
                if high >> 26 == 15 and low >> 26 in (9, 13) and (high >> 16 & 31) == (low >> 21 & 31):
                    immediate = low & 0xFFFF
                    address = (high & 0xFFFF) << 16
                    address = (
                        address | immediate
                        if low >> 26 == 13
                        else address + (immediate - 0x10000 if immediate & 0x8000 else immediate)
                    )
                    addresses.add(address & 0xFFFFFFFF)
            for pool in pools:
                if pool.section in artifact["private_rodata"]:
                    continue
                start = bases[pool.section] + pool.offset
                if any(start <= address < start + pool.size for address in addresses):
                    owners[(pool.section, pool.offset)].update((symbol.name, *symbol.aliases))
    return TrialObject(
        obj,
        family,
        function,
        text_base,
        aligned,
        rodata_reader(read_memory, obj, bases),
        {key: tuple(sorted(names)) for key, names in owners.items()},
    )
