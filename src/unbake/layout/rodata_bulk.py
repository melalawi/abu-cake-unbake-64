"""One ownership census and one transaction for all resident constant storage.

Keep the existing access expressions, including volatile and aggregate reads.
Their configured absolute symbols refer to native C storage at the same runtime
address. Exported storage identifiers carry address and extent for the layout
writer, which validates every byte; they are not source aliases or assembly.
"""

import bisect
import math
import struct
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from unbake.layout import split
from unbake.layout.rodata_migrate import literal
from unbake.layout.rodata_owners import Census, Constant, scan
from unbake.project.config import Held, Project
from unbake.project_tools.elf import Object
from unbake.project_tools.literal_layout import storage


@dataclass
class Group:
    start: int
    end: int
    address: int
    row: split.Row
    objects: list[Constant] = field(default_factory=list)
    host: str = ""


@dataclass
class Bulk:
    edits: list[split.Edit]
    migrated: dict[str, list[dict[str, object]]]
    resident: dict[str, list[dict[str, object]]]
    bytes_left: dict[str, int]

    def document(self) -> dict[str, object]:
        return {"migrated": self.migrated, "resident": self.resident, "resident_bytes": self.bytes_left}


def definition(item: Constant, data: bytes) -> str:
    """Emit complete initialized C objects, including nonfinite bit patterns."""
    name = f"unbake_rodata_{item.address:08X}_{len(data):X}"
    if item.kind in ("float", "double") and len(data) == (4 if item.kind == "float" else 8):
        value = struct.unpack(">f" if item.kind == "float" else ">d", data)[0]
        if math.isfinite(value):
            return f"const {item.kind} {name} = {literal(data, item.kind)};\n"
    if item.kind == "jump table" and len(data) % 4 == 0:
        values = ", ".join(f"0x{word[0]:08X}U" for word in struct.iter_unpack(">I", data))
        return f"const unsigned int {name}[] = {{{values}}};\n"
    values = ", ".join(f"0x{value:02X}" for value in data)
    return f"const unsigned char {name}[] = {{{values}}};\n"


def macro(project: Project, version: str) -> str:
    candidates = [
        name
        for name in project.version(version).macros
        if name.startswith("VERSION_")
        and all(name not in project.version(other).macros for other in project.versions if other != version)
    ]
    if len(candidates) != 1:
        raise Held("rodata", f"{version}: required one unique VERSION macro")
    return candidates[0]


def groups(project: Project, census: Census) -> tuple[list[Group], list[dict[str, object]]]:
    """Partition resident rows once, keeping writable data out of constant pools."""
    configured = project.version(census.version)
    _, _, segments = split.layout(configured.split)
    rows = [row for segment in segments for row in segment.rows if row.kind == "bin"]
    starts = [row.start for row in rows]
    ends = {row.line: split.end(row) for row in rows}
    result: list[Group] = []
    refused = []
    image = configured.baserom.read_bytes()
    for item in census.objects:
        if not item.owners:
            continue
        spans = [span for span in census.spans if span.address <= item.address and item.end <= span.stop]
        if len(spans) != 1:
            raise Held("rodata", f"0x{item.address:08X}: ambiguous census span")
        span = spans[0]
        offset = span.start + item.address - span.address
        at = bisect.bisect_right(starts, offset) - 1
        if at < 0 or offset + item.end - item.address > ends[rows[at].line]:
            # Native rodata already has a load-producing selector.
            continue
        row = rows[at]
        if item.writes:
            refused.append({"address": item.address, "owners": sorted(item.owners), "class": "writable data"})
            continue
        stop = offset + item.end - item.address
        owners = item.owners
        previous = result[-1] if result else None
        if (
            previous is not None
            and previous.row.line == row.line
            and previous.objects[-1].owners == owners
            and 0 <= offset - previous.end <= 3
            and not any(image[previous.end : offset])
        ):
            previous.end = stop
            previous.objects.append(item)
        else:
            result.append(Group(offset, stop, item.address, row, [item]))
    return result, refused


def hosts(project: Project, census: Census, selected: list[Group]) -> None:
    """Use a common owner file when available; allocate distinct storage otherwise.

    Distinct resident runs need distinct input sections. A matched C unit with
    no existing data is a storage host for a run whose owners are assembly units
    or whose common file already supplies another run. No function is rewritten.
    """
    if not selected:
        return
    available: dict[str, split.Function] = {}
    owner_paths: dict[str, str] = {}
    for function in census.functions:
        for name in (function.name, Path(function.path).stem, *function.aliases):
            owner_paths[name] = function.path
        if function.kind != "c" or function.path in available:
            continue
        source = project.src / (function.path + ".c")
        path = project.build_link(census.version) / "obj/src" / (function.path + ".o")
        if not source.is_file() or not path.is_file():
            continue
        obj = Object(path)
        occupied = False
        for name in (".rdata", ".rodata", ".data", ".bss"):
            index = obj.section(name)
            if index is None or not obj.sections[index][5]:
                continue
            anchors = storage(obj, index) if name in (".rdata", ".rodata") else []
            covered = {at for own, _, size in anchors for at in range(own, own + size)}
            # After restoring source/split inputs for a whole-run retry, derived
            # objects may still contain the previous run's generated storage.
            # It must not disqualify the original empty storage host.
            if (
                not anchors
                or obj.relocations(index)
                or any(value and at not in covered for at, value in enumerate(obj.content(index)))
            ):
                occupied = True
                break
        if occupied:
            continue
        available[function.path] = function
    ordered = sorted(available)
    used: set[str] = set()
    for group in selected:
        owners = set.union(*(item.owners for item in group.objects))
        paths = {owner_paths[name] for name in owners if name in owner_paths}
        preferred = next((path for path in sorted(paths) if path in available and path not in used), None)
        if preferred is None:
            preferred = next((name for name in ordered if name not in used), None)
        if preferred is None:
            raise Held("rodata", f"VERSION {census.version}: no C storage host for 0x{group.address:08X}")
        group.host = preferred
        used.add(preferred)


def layout(project: Project, census: Census, selected: list[Group]) -> split.Edit:
    path = project.version(census.version).split
    before, lines, _ = split.layout(path)
    byrow: dict[int, list[Group]] = defaultdict(list)
    for group in selected:
        byrow[group.row.line].append(group)
    for line, values in byrow.items():
        row = values[0].row
        cursor = row.start
        output = []
        runtime = values[0].address + row.start - values[0].start
        if split.address(row, path) != runtime:
            if any(other.kind != "bin" for other in row.segment.rows[row.segment.rows.index(row) + 1 :]):
                raise Held("rodata", "runtime copy requires trailing resident rows")
            output.extend(
                [
                    f"  - name: constants_{runtime:08X}\n",
                    "    type: code\n",
                    f"    start: 0x{row.start:X}\n",
                    f"    vram: 0x{runtime:X}\n",
                    "    subalign: 1\n",
                    "    subsegments:\n",
                ]
            )
        else:
            if any(other.kind in ("asm", "c") for other in row.segment.rows):
                raise Held("rodata", "byte storage requires a separate resident segment")
            for header_line in range(row.segment.rows[0].line - 1, -1, -1):
                if lines[header_line].startswith("  - "):
                    break
                if lines[header_line].strip().startswith("subalign:"):
                    lines[header_line] = "    subalign: 1\n"
        for group in values:
            if cursor > group.start:
                raise Held("rodata", f"0x{group.address:08X}: overlapping storage runs")
            if cursor < group.start:
                output.append(f"{row.match['indent']}- [0x{cursor:X}, bin, data/resident_{cursor:08X}]\n")
            output.append(f"{row.match['indent']}- [0x{group.start:X}, .rodata, {group.host}]\n")
            cursor = group.end
        if cursor < split.end(row):
            output.append(f"{row.match['indent']}- [0x{cursor:X}, bin, data/resident_{cursor:08X}]\n")
        lines[line] = "".join(output)
    after = "".join(lines)
    if "auto_link_sections:" not in after:
        after = after.replace("options:\n", 'options:\n  auto_link_sections: [".data", ".bss"]\n', 1)
    split.parse_layout(path, after)
    return split.Edit(path, before, after, (census.version,))


def migrate_all(project: Project, censuses: dict[str, Census] | None = None) -> Bulk:
    """Compose all VERSION edits without writing any project input."""
    censuses = censuses if censuses is not None else {version: scan(project, version) for version in project.versions}
    edits = []
    definitions: dict[str, dict[str, str]] = defaultdict(dict)
    migrated: dict[str, list[dict[str, object]]] = {}
    resident = {}
    bytes_left = {}
    for version in project.versions:
        census = censuses[version]
        # Existing native local pools can have a short final string. Their
        # original SUBALIGN padding was implicit; byte-aligned bulk storage
        # requires that final byte (which need not be zero) to be explicit C.
        image = project.version(version).baserom.read_bytes()
        for segment in split.layout(project.version(version).split)[2]:
            for row in segment.rows:
                if row.kind != ".rodata":
                    continue
                objpath = project.build_link(version) / "obj/src" / (row.path + ".o")
                if not objpath.is_file():
                    continue
                obj = Object(objpath)
                emitted = sum(
                    obj.sections[index][5] for name in (".rdata", ".rodata") if (index := obj.section(name)) is not None
                )
                extent = split.end(row) - row.start
                if emitted >= extent:
                    continue
                address = split.address(row, project.version(version).split) + emitted
                item = Constant(address, address + extent - emitted, "other", "resident", "native trailing extent", [])
                definitions[row.path][version] = definition(item, image[row.start + emitted : split.end(row)])
        selected, refused = groups(project, census)
        hosts(project, census, selected)
        edits.append(layout(project, census, selected))
        migrated[version] = []
        for group in selected:
            text = ""
            for item in group.objects:
                offset = group.start + item.address - group.address
                text += definition(item, image[offset : offset + item.end - item.address])
                migrated[version].append(
                    {
                        "address": item.address,
                        "size": item.end - item.address,
                        "kind": item.kind,
                        "owners": sorted(item.owners),
                        "storage": group.host,
                    }
                )
            # Explicit leading/trailing alignment has storage identity too.
            first, last = group.objects[0], group.objects[-1]
            for address, end in ((group.address, first.address), (last.end, group.address + group.end - group.start)):
                if address < end:
                    item = Constant(address, end, "other", "resident", "zero alignment", [])
                    text += definition(item, bytes(end - address))
            definitions[group.host][version] = text
        resident[version] = refused
        before, _, segments = split.layout(project.version(version).split)
        bytes_left[version] = sum(
            split.end(row) - row.start
            for segment in segments
            for row in segment.rows
            if row.kind == "bin" and any(span.start <= row.start < span.end for span in census.spans)
        ) - sum(group.end - group.start for group in selected)
    for host, variants in sorted(definitions.items()):
        path = project.src / (host + ".c")
        before = split.read(path)
        if any(candidate.is_symlink() for candidate in (path, *path.parents)):
            raise Held("rodata", f"{path}: cannot edit through a symlink")
        text = "\n/* Native resident constant storage; absolute access symbols retain their addresses. */\n"
        bytext: dict[str, list[str]] = defaultdict(list)
        for version, content in variants.items():
            bytext[content].append(version)
        if len(bytext) == 1 and len(variants) == len(project.versions):
            text += next(iter(bytext))
        else:
            for index, (content, versions) in enumerate(bytext.items()):
                text += (
                    ("#if " if index == 0 else "#elif ")
                    + " || ".join(f"defined({macro(project, version)})" for version in versions)
                    + "\n"
                    + content
                )
            text += "#endif\n"
        edits.append(split.Edit(path, before, before + text, tuple(variants)))
    return Bulk(edits, migrated, resident, bytes_left)
