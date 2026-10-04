"""Measure and prove existing C units before porting their version split rows."""

from __future__ import annotations

from pathlib import Path

from unbake.config import Held, Project
from unbake.layout import split
from unbake.objects.elf import Object


def relocation_masks(path: Path, size: int) -> dict[int, int]:
    """Only fields recorded by the ELF relocation table may be ignored."""
    obj = Object(path)
    section = obj.section(".text")
    if section is None:
        raise Held("port", f"{path}: missing .text")
    masks: dict[int, int] = {}
    for offset, kind, _ in obj.relocations(section):
        if offset >= size:
            continue
        mask = {2: 0xFFFFFFFF, 4: 0x03FFFFFF, 5: 0xFFFF, 6: 0xFFFF, 10: 0xFFFF}.get(kind)
        if mask is None or offset % 4:
            raise Held("port", f"{path}: unsupported relocation {kind} at {offset:#x}")
        masks[offset] = masks.get(offset, 0) | mask
    return masks


def object_path(project: Project, function: split.Function) -> Path:
    return (
        project.build_link(function.version)
        / "obj"
        / ("src" if function.kind == "c" else "asm")
        / (function.path + ".o")
    )


def identity(project: Project, source: split.Function, target: split.Function) -> tuple[str, int, str]:
    left, right = split.words(project, source), split.words(project, target)
    if len(left) != len(right):
        return (
            "different",
            abs(len(left) - len(right)) // 4,
            f"target size differs (source {len(left)} bytes, target {len(right)} bytes)",
        )
    if left == right:
        return "identical", 0, ""
    try:
        masks = relocation_masks(object_path(project, source), len(left))
        for offset, mask in relocation_masks(object_path(project, target), len(right)).items():
            masks[offset] = masks.get(offset, 0) | mask
    except (OSError, ValueError, Held) as error:
        return "unknown", -1, str(error)
    differences = sum(
        (int.from_bytes(left[offset : offset + 4], "big") ^ int.from_bytes(right[offset : offset + 4], "big"))
        & (0xFFFFFFFF ^ masks.get(offset, 0))
        != 0
        for offset in range(0, len(left), 4)
    )
    return ("relocations" if differences == 0 else "different"), differences, ""


def functions(project: Project, version: str) -> list[split.Function]:
    """Index symbol aliases once rather than scanning all symbols for every row."""
    configured = project.version(version)
    _, _, segments = split.layout(configured.split)
    _, symbols = split.symbols(configured.symbols)
    aliases: dict[int, list[str]] = {}
    for name, (address, _, _) in symbols.items():
        aliases.setdefault(address, []).append(name)
    result = []
    for segment in segments:
        for index, row in enumerate(segment.rows):
            if row.kind not in ("asm", "c"):
                continue
            address = split.address(row, configured.split)
            stem = Path(row.path).name
            names = aliases.get(address, [])
            name = stem if stem in names or not names else names[0]
            result.append(
                split.Function(
                    version,
                    name,
                    row.start,
                    segment.rows[index + 1].start if index + 1 < len(segment.rows) else int(segment.end or 0),
                    address,
                    row.path,
                    row.kind,
                    tuple(dict.fromkeys((stem, *names))),
                )
            )
    return result
