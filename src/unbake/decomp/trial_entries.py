"""Compare complete owning text and validate every committed global entry."""

from __future__ import annotations

import hashlib
import struct
from pathlib import Path

from unbake.layout import entries, split
from unbake.project.config import Held, Policy, Project
from unbake.project_tools.elf import Object, Symbol


def functions(obj: Object) -> list[Symbol]:
    return [s for table in obj.symbols.values() for s in table if s["section"] and s["info"] & 15 == 2]


def target(project: Project, policy: Policy, source: Path, version: str, generation: Path, first: Path) -> Path:
    """Combine only contiguous, native owners defined by this C item."""
    from unbake.decomp.trial_compile import run_tool
    from unbake.decomp.trial_target import target_object

    group = entries.owners(project, policy, source, version)
    if len(group) == 1:
        return first
    objects = [first]
    for row in group[1:]:
        relative = Path("obj/asm") / (row.path + ".o")
        path = generation / relative
        if not path.is_file():
            path = target_object(project, row.name, version)
            if not path.is_relative_to(generation):
                raise Held("try", "trial.entries_target: secondary entry generation changed")
        objects.append(path)
    identity = hashlib.sha256(b"".join(path.read_bytes() for path in objects)).hexdigest()
    directory = project.work / "entry-targets" / identity
    directory.mkdir(parents=True, exist_ok=True)
    output = directory / "target.o"
    if not output.exists():
        script = directory / "text.ld"
        script.write_text("SECTIONS { .text : SUBALIGN(4) { *(.text) } }\n")
        run_tool(
            [str(policy.mips_ld), "-r", "-T", str(script), "-o", str(output), *map(str, objects)], directory, "try"
        )
    obj = Object(output)
    symbols = {s["name"]: s for s in functions(obj)}
    for row in group:
        symbol = symbols.get(row.name)
        if symbol is None or symbol["value"] != row.start - group[0].start:
            raise Held("try", f"trial.entries_target: {row.name}: linked target entry moved")
    return output


def inventory(project: Project, policy: Policy, source: Path, version: str) -> dict[str, object]:
    group = entries.owners(project, policy, source, version)
    return {
        "start": group[0].start,
        "end": group[-1].end,
        "rows": [row.path for row in group],
        "entries": [
            {"name": name, "offset": row.start - group[0].start + offset}
            for row in group
            for name, offset in (row.entries or ((row.name, 0),))
        ],
    }


def view(path: Path, function: str, size: int, output: Path) -> Path:
    """Give objdiff one interval view; original objects and entry evidence stay intact."""
    obj = Object(path)
    for index, table in obj.symbols.items():
        for ordinal, symbol in enumerate(table):
            if symbol["info"] & 15 != 2 or not symbol["section"]:
                continue
            at = obj.sections[index][4] + ordinal * 16
            if symbol["name"] == function:
                struct.pack_into(">I", obj.data, at + 8, size)
            elif symbol["section"] == obj.section(".text"):
                obj.data[at + 12] &= 0xF0
    output.write_bytes(obj.data)
    return output


def comparison_views(
    project: Project, policy: Policy, source: Path, version: str, target: Path, candidate: Path, directory: Path
) -> tuple[Path, Path, list[str]]:
    """Require mapped names/offsets, and compare bytes beyond the first symbol too."""
    group = entries.owners(project, policy, source, version)
    size = group[-1].end - group[0].start
    left, right = Object(target), Object(candidate)
    primary = next(s for s in functions(left) if s["name"] == source.stem)
    left_symbols = [s for s in functions(left) if s["section"] == primary["section"] and s["value"] < size]
    outside = {s["name"] for s in functions(left) if s["value"] >= size}
    right_symbols = [s for s in functions(right) if s["name"] not in outside]
    if len(group) == 1 and len(left_symbols) == 1 and len(right_symbols) == 1:
        return target, candidate, []
    mapped = split.symbols(project.version(version).symbols)[1]
    failures = []
    inferred = [s for s in left_symbols if s["name"] not in mapped and s["name"] != source.stem]
    for symbol in left_symbols:
        if symbol["name"] not in mapped and symbol["name"] != source.stem:
            # Disassembly can label unreachable epilogues as separate functions.
            # Such a fragment must still be covered by the compiled owner bytes.
            continue
        matches = [s for s in right_symbols if s["name"] == symbol["name"]]
        if (
            len(matches) != 1
            or matches[0]["value"] != symbol["value"]
            or matches[0]["info"] >> 4 != symbol["info"] >> 4
        ):
            failures.append(f"entry {symbol['name']}: missing or moved from +0x{symbol['value']:X}")
        elif matches[0]["size"] != symbol["size"]:
            stop = matches[0]["value"] + matches[0]["size"]
            covered = [s for s in inferred if symbol["value"] + symbol["size"] <= s["value"] < stop]
            if not covered or stop != max(s["value"] + s["size"] for s in covered):
                failures.append(f"entry {symbol['name']}: compiled symbol size differs")
    for symbol in right_symbols:
        if not symbol["info"] >> 4:
            continue
        matches = [s for s in left_symbols if s["name"] == symbol["name"]]
        if not matches:
            failures.append(f"entry {symbol['name']}: unexpected global definition")
        elif symbol["value"] != matches[0]["value"] or symbol["info"] >> 4 != matches[0]["info"] >> 4:
            failures.append(f"entry {symbol['name']}: compiled entry offset or binding differs")
    section = right.section(".text")
    candidate_size = max((s["value"] + s["size"] for s in right_symbols if s["section"] == section), default=0)
    if section is not None and any(right.content(section)[candidate_size:]):
        candidate_size = len(right.content(section))
    return (
        view(target, source.stem, size, directory / "target-interval.o"),
        view(candidate, source.stem, candidate_size, directory / "candidate-interval.o"),
        failures,
    )
