"""Attribute cartridge failures from linked object extents and symbol evidence."""

from __future__ import annotations

import argparse
import json
import re
import struct
from pathlib import Path

from unbake.layout import split
from unbake.config import Project
from unbake.project_tools import extract, layout
from unbake.objects.elf import Object

_CONTRIBUTION = re.compile(r"^\s+(\.[\w.]+)\s*\n?\s*(0x[\da-fA-F]+)\s+(0x[\da-fA-F]+)\s+(obj/[^\s]+\.o)\s*$", re.M)


def _local(line: str) -> str:
    """Drop directory prefixes so a compiler diagnostic keeps its message, not its paths."""
    return re.sub(r"(?<![\w.])/?(?:[^\s/:]+/)+", "", line.strip())


def material(obj: Object, address: int, size: int) -> bytes | None:
    for index, section in enumerate(obj.sections):
        if section[2] & 2 and section[1] != 8 and section[3] <= address < address + size <= section[3] + section[5]:
            start = address - section[3]
            return obj.content(index)[start : start + size]
    return None


def diagnose(
    project: Project,
    failures: list[str],
    generations: dict[str, Path],
    names: set[str],
    reference: tuple[Project, dict[str, Path]] | None = None,
) -> dict[str, list[str]]:
    """Compare every allocated input object and defined symbol, retaining all faults."""
    faults: dict[str, list[str]] = {}
    relocated: dict[str, list[str]] = {}

    def blame(name: str, detail: str) -> None:
        if name in names:
            faults.setdefault(name, []).append(detail)

    def masked(obj: Object, section: int, left: bytes, right: bytes, base: int) -> bool:
        """Whether two section byte runs differ only inside relocated words."""
        words = {offset - base for offset, _, _ in obj.relocations(section)}
        return all(
            left[at : at + 4] == right[at : at + 4] or at in words for at in range(0, min(len(left), len(right)), 4)
        )

    for version in failures:
        if version not in generations:
            continue
        generation = generations[version]
        log_path = generation / "build.log"
        log = log_path.read_text(errors="replace") if log_path.is_file() else ""
        context: list[str] = []
        # A batch compile reports each failed source on one line; the compiler's
        # own diagnostic lines follow it until the next source or driver line.
        compiled: dict[str, list[str]] = {}
        current: list[str] | None = None
        for line in log.splitlines():
            failed = re.match(r"^(?:\S*/)?src/([A-Za-z_]\w*)\.c: (.*)$", line)
            if failed:
                current = compiled.setdefault(failed[1], [_local(failed[2])])
                continue
            if current is not None and not re.match(r"^(make|python3|HELD|OK|\S+\.py)\b", line):
                if re.search(r"(?i)\berror\b|:\d+:", line):
                    current.append(_local(line))
                continue
            current = None
            if "in function" in line:
                context = re.findall(r"obj/src/([^\s/:()]+)\.o", line)
            if any(word in line for word in ("Error:", "HELD(compile)", "batch objects failed")):
                for name in re.findall(r"src/([^\s/:()]+)\.c", line):
                    blame(name, f"{version}: compile diagnostic: {line}")
            if "submit.reuse_inputs:" in line:
                for name in re.findall(r"src/([^\s/:()]+)\.c", line):
                    blame(name, f"{version}: {line}")
            if any(
                word in line for word in ("HELD(", "undefined reference", "multiple definition", "overlap", "overflow")
            ):
                for name in context + re.findall(r"obj/src/([^\s/:()]+)\.o", line):
                    blame(name, f"{version}: link diagnostic: {line}")
                context = []
        for name, lines in compiled.items():
            blame(name, f"{version}: compile diagnostic: {'; '.join(lines)[:600]}")
        # Object-relative checks run even when another unit prevented linking.
        # A compile error must not conceal independent byte or binding failures.
        intervals = extract.unit_ranges(project.version(version).split.read_text())
        image = project.version(version).baserom.read_bytes()
        _, known = split.symbols(project.version(version).symbols)
        addresses = generation / "symbol-addresses.txt"
        bindings = set(known)
        if addresses.is_file():
            bindings.update(line.split()[0] for line in addresses.read_text().splitlines() if line.split())
        objects = {}
        for function in sorted(names & intervals.keys()):
            path = generation / "obj/src" / (function + ".o")
            if path.is_file() and function not in compiled:
                objects[function] = Object(path)
        definitions = {
            symbol["name"]
            for obj in objects.values()
            for table in obj.symbols.values()
            for symbol in table
            if symbol["section"] != 0
        }
        alignments = {
            Path(row.path).name: int(row.match["align"], 0)
            for segment in split.layout(project.version(version).split)[2]
            for row in segment.rows
            if row.kind == "c" and row.match["align"]
        }
        script_path = generation / f"{project.name}.ld"
        script = script_path.read_text() if script_path.is_file() else ""
        configured = (
            json.loads((project.tools / "build.json").read_text()).get("resident_mappings", {})
            if (project.tools / "build.json").is_file()
            else {}
        )
        mappings = layout.resident_mappings(configured.get(version, []))
        targets = contribution_targets(project, version)
        storage_targets: dict[str, list[tuple[str, tuple[int, int]]]] = {}
        for (section_name, object_name), storage_span in targets.items():
            if section_name != ".text":
                storage_targets.setdefault(object_name, []).append((section_name, storage_span))
        strong: dict[str, list[str]] = {}
        for function, obj in objects.items():
            for table in obj.symbols.values():
                for symbol in table:
                    if symbol["section"] not in (0, 0xFFF2) and symbol["info"] >> 4 == 1:
                        strong.setdefault(symbol["name"], []).append(function)
        for symbol_name, providers in strong.items():
            if len(set(providers)) > 1:
                for function in providers:
                    blame(function, f"{version}: multiple definition of {symbol_name}: " + ", ".join(providers))
        reference_rows = split.functions(reference[0], version) if reference is not None else []
        for function, obj in objects.items():
            interval = intervals[function]
            if reference is not None:
                # Automatic data/BSS selectors have no separate split row. Use
                # the proved original providers of this same owning text span.
                baseline_providers = [
                    row for row in reference_rows if interval["start"] <= row.start < row.end <= interval["end"]
                ]
                for section_name in (".data", ".rodata", ".bss"):
                    section_index = obj.section(section_name)
                    if section_index is None or not obj.sections[section_index][5]:
                        continue
                    if (section_name, f"obj/src/{function}.o") in targets:
                        continue
                    expected_size = 0
                    complete = bool(baseline_providers)
                    for row in baseline_providers:
                        prefix = "obj/src" if row.kind == "c" else "obj/asm"
                        baseline = reference[1][version] / prefix / (row.path + ".o")
                        if not baseline.is_file():
                            complete = False
                            break
                        baseline_obj = Object(baseline)
                        baseline_index = baseline_obj.section(section_name)
                        if baseline_index is not None:
                            expected_size += baseline_obj.sections[baseline_index][5]
                    # Compiler constants installed as resident overlays are
                    # proved by layout, and do not consume the automatic span.
                    selector = re.escape(f"obj/src/{function}.o") + r"\s*\(" + re.escape(section_name) + r"\)"
                    if complete and re.search(selector, script) and obj.sections[section_index][5] != expected_size:
                        blame(
                            function,
                            f"{version}: shift origin object obj/src/{function}.o {section_name}: "
                            f"size {obj.sections[section_index][5]}, target span {expected_size}",
                        )
            index = obj.section(".text")
            if index is None:
                blame(function, f"{version}: object obj/src/{function}.o: missing .text")
                continue
            code = obj.content(index)
            target = image[interval["start"] : interval["end"]]
            if len(code) > len(target):
                blame(
                    function,
                    f"{version}: object obj/src/{function}.o: .text size {len(code)} exceeds target span {len(target)}",
                )
            alignment = alignments.get(function, 1)
            consumed = -(-(interval["address"] + len(code)) // alignment) * alignment - interval["address"]
            if len(code) < len(target) and consumed != len(target):
                blame(
                    function,
                    f"{version}: shift origin object obj/src/{function}.o .text: size {len(code)}, "
                    f"consumed {consumed} bytes, target span {len(target)}",
                )
            relocations = {offset for offset, _, _ in obj.relocations(index)}
            for at in range(0, min(len(code), len(target)), 4):
                if at not in relocations and code[at : at + 4] != target[at : at + 4]:
                    blame(
                        function,
                        f"{version}: object obj/src/{function}.o .text+0x{at:X}: symbol {function}: "
                        f"expected {target[at : at + 4].hex()}, produced {code[at : at + 4].hex()}",
                    )
                    break
            unknown = sorted(
                {
                    symbol["name"]
                    for table in obj.symbols.values()
                    for symbol in table
                    if symbol["section"] == 0
                    and symbol["name"]
                    and symbol["info"] >> 4 == 1
                    and symbol["name"] not in bindings | definitions
                }
            )
            if unknown:
                blame(function, f"{version}: undefined reference to {', '.join(unknown)}")
            # Allocated storage rows get their own object-relative proof too;
            # a failed compile elsewhere must not conceal data or BSS faults.
            for section_name, (start, end) in storage_targets.get(f"obj/src/{function}.o", []):
                section_index = obj.section(section_name)
                if section_index is None:
                    continue
                size = obj.sections[section_index][5]
                if size != end - start:
                    blame(
                        function,
                        f"{version}: shift origin object {object_name} {section_name}: "
                        f"size {size}, target span {end - start}",
                    )
                if obj.sections[section_index][1] == 8:
                    continue
                content = obj.content(section_index)
                for segment in split.layout(project.version(version).split)[2]:
                    if "start" not in segment.fields or "vram" not in segment.fields or segment.end is None:
                        continue
                    offset = int(segment.fields["start"], 0) + start - int(segment.fields["vram"], 0)
                    if not int(segment.fields["start"], 0) <= offset < offset + size <= segment.end:
                        continue
                    expected_bytes = image[offset : offset + size]
                    if content != expected_bytes and not masked(obj, section_index, content, expected_bytes, 0):
                        blame(function, f"{version}: object {object_name} {section_name}: bytes differ at target span")
                    break
            if script:
                try:
                    layout.place_object(
                        argparse.Namespace(build=generation),
                        f"obj/src/{function}.o",
                        script,
                        intervals,
                        image,
                        mappings,
                        [],
                        False,
                    )
                except (OSError, ValueError, KeyError, struct.error) as error:
                    blame(function, f"{version}: object obj/src/{function}.o: {error}")

        elf_path = generation / f"{project.name}.elf"
        map_path = generation / f"{project.name}.map"
        if not map_path.is_file():
            continue
        targets = contribution_targets(project, version)
        contributions = [
            (section, int(address, 16), int(size, 16), path)
            for section, address, size, path in _CONTRIBUTION.findall(map_path.read_text())
            if int(address, 16) and int(size, 16)
        ]
        # Compare consumed spans in link order, including alignment/fill before
        # the next provider. Equal incoming and outgoing displacement means a
        # victim. A change in displacement identifies the contributing origin.
        origins: set[str] = set()
        for i, (section, address, size, path) in enumerate(contributions):
            span = targets.get((section, path))
            if span is None:
                continue
            start, end = span
            following = next((row for row in contributions[i + 1 :] if (row[0], row[3]) in targets), None)
            consumed = size
            expected_size = end - start
            if following is not None:
                next_start, _ = targets[following[0], following[3]]
                # Output sections and overlays can reset the location counter.
                if next_start == end and following[1] >= address + size:
                    consumed = following[1] - address
            if consumed != expected_size:
                name = Path(path).stem
                origins.add(name)
                blame(
                    name,
                    f"{version}: shift origin object {path} {section}: consumed {consumed} bytes, "
                    f"target span {expected_size}; address 0x{address:08X}, target 0x{start:08X}",
                )
        if not elf_path.is_file():
            continue
        elf = Object(elf_path)
        _, _, segments = split.layout(project.version(version).split)

        def resident(
            address: int, size: int, spans: list[split.Segment] = segments, rom: bytes = image
        ) -> bytes | None:
            for segment in spans:
                if "vram" in segment.fields and "start" in segment.fields and segment.end is not None:
                    start = int(segment.fields["start"], 0)
                    offset = start + address - int(segment.fields["vram"], 0)
                    if start <= offset < offset + size <= segment.end:
                        return rom[offset : offset + size]
            return None

        linked = {symbol["name"]: symbol for table in elf.symbols.values() for symbol in table}
        for section, address, size, path in contributions:
            function = Path(path).stem
            if function not in objects:
                continue
            obj = objects[function]
            index = obj.section(section)
            if index is None:
                continue
            span = targets.get((section, path))
            if span is None:
                # Explicit defined data symbols can anchor sections absent from
                # the text rows, including BSS with no ROM material.
                anchors = {
                    known[symbol["name"]][0] - symbol["value"]
                    for table in obj.symbols.values()
                    for symbol in table
                    if symbol["section"] == index and symbol["name"] in known
                }
                if len(anchors) == 1:
                    target_address = anchors.pop()
                else:
                    continue
            else:
                target_address = span[0]
            if address != target_address and function not in origins:
                # Reprove placement victims after origins have been removed.
                continue
            actual, expected = material(elf, address, size), resident(target_address, size)
            if actual is not None and expected is not None and actual != expected:
                at = next(i for i, (a, b) in enumerate(zip(expected, actual, strict=True)) if a != b)
                detail = (
                    f"{version}: object {path} {section}+0x{at:X}: "
                    f"expected {expected[at : at + 16].hex()}, produced {actual[at : at + 16].hex()}"
                )
                if masked(obj, index, expected, actual, 0):
                    relocated.setdefault(function, []).append(detail)
                else:
                    blame(function, detail)
            for table in obj.symbols.values():
                for symbol in table:
                    name = symbol["name"]
                    if symbol["section"] != index or name not in known or name not in linked:
                        continue
                    relative = linked[name]["value"] - address
                    expected_relative = known[name][0] - target_address
                    if relative != expected_relative:
                        blame(
                            function,
                            f"{version}: defined symbol {name}: section offset 0x{relative:X}, "
                            f"target 0x{expected_relative:X}",
                        )
    return faults or relocated


def contribution_targets(project: Project, version: str) -> dict[tuple[str, str], tuple[int, int]]:
    """Explicit input section extents from the staged owning rows, in VRAM."""
    targets = {}
    _, _, segments = split.layout(project.version(version).split)
    c_units = {Path(row.path).name for row in split.functions(project, version) if row.kind == "c"}
    for segment in segments:
        if "vram" not in segment.fields or "start" not in segment.fields:
            continue
        bias = int(segment.fields["vram"], 0) - int(segment.fields["start"], 0)
        for i, row in enumerate(segment.rows):
            end = segment.rows[i + 1].start if i + 1 < len(segment.rows) else segment.end
            if end is None:
                continue
            kind = row.kind.lstrip(".")
            if kind in ("asm", "c"):
                section = ".text"
                path = f"obj/{'src' if kind == 'c' else 'asm'}/{row.path}.o"
            elif kind in ("data", "rodata", "rdata", "bss"):
                section = "." + kind
                name = Path(row.path).name
                path = f"obj/src/{name}.o" if name in c_units else f"obj/asm/data/{row.path}.{kind}.o"
            else:
                continue
            targets[section, path] = row.start + bias, end + bias
            if section == ".rodata" and path.startswith("obj/src/"):
                targets[".rdata", path] = row.start + bias, end + bias
    for name, interval in extract.unit_ranges(project.version(version).split.read_text()).items():
        for row in interval.get("rodata_slices", []):
            if row["path"].startswith("rodata/"):
                targets[f".unbake_pool_{row['address']:08X}", f"obj/src/{name}.o"] = (
                    row["address"],
                    row["address"] + row["end"] - row["start"],
                )
    return targets
