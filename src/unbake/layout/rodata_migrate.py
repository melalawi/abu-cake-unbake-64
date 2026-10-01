"""Compose native split and plain C edits for a function's private constants."""

import math
import re
import struct
from functools import partial
from pathlib import Path

from unbake.decomp.rom import RomReader, project_reader
from unbake.layout import split
from unbake.layout.rodata_owners import Census, Constant, private, scan
from unbake.layout.rodata_references import Reference, collect
from unbake.layout.rodata_switch import lower
from unbake.project.config import Held, Project
from unbake.project_tools.elf import Object

DECLARATION = re.compile(
    r"^[ \t]*extern\s+(?P<type>(?:const\s+)?[A-Za-z_]\w*(?:\s*\*)?)\s+"
    r"(?P<name>[A-Za-z_]\w*)\s*(?P<array>\[[^\]]*\])?\s*;[^\S\n]*\n?",
    re.M,
)
ADDRESS = r"(?<!&)&\s*\b"


def literal(data: bytes, kind: str) -> str:
    if kind == "string":
        return (
            '"'
            + "".join(
                '\\"' if c == 34 else "\\\\" if c == 92 else chr(c) if 32 <= c < 127 else f"\\{c:03o}"
                for c in data[:-1]
            )
            + '"'
        )
    if kind not in ("float", "double"):
        raise Held("rodata", f"{kind}: no scalar literal")
    value = struct.unpack(">f" if kind == "float" else ">d", data)[0]
    if not math.isfinite(value):
        raise Held("rodata", "nonfinite literal requires explicit source representation")
    result = format(value, ".9g" if kind == "float" else ".17g")
    if "." not in result and "e" not in result.lower():
        result += ".0"
    result += "f" if kind == "float" else ""
    return "(" + result + ")" if result.startswith("-") else result


def replacement_text(value: str, match: re.Match[str]) -> str:
    return value


def array_literal(values: dict[int, str], match: re.Match[str]) -> str:
    spelling = match[1]
    index = int(spelling, 16 if spelling.lower().startswith("0x") else 8 if spelling.startswith("0") else 10)
    if index not in values:
        raise Held("rodata", f"array element {index}: missing private typed evidence")
    return values[index]


def rewrite(
    project: Project, version: str, source: str, objects: list[Constant], references: list[Reference], reader: RomReader
) -> str:
    _, symbols = split.symbols(project.version(version).symbols)
    byaddress = {o.address: o for o in objects}
    substitutions: dict[str, str] = {}
    arrays: dict[str, dict[int, str]] = {}
    addresses: dict[str, str] = {}
    removed: list[tuple[int, int]] = []
    declarations: dict[str, str] = {}
    code = re.sub(r"/\*.*?\*/|//[^\n]*", "", source, flags=re.S)
    for match in DECLARATION.finditer(source):
        name = match["name"]
        entry = symbols.get(name)
        item = byaddress.get(entry[0]) if entry is not None else next((o for o in objects if name in o.names), None)
        if item is None:
            addressed = [r.address for r in references if r.symbol == name and r.owner in objects[0].owners]
            if (
                entry is not None
                and addressed
                and all(a in byaddress and byaddress[a].kind in ("float", "double") for a in addressed)
                and re.search(ADDRESS + re.escape(name) + r"\b", code)
            ):
                first = byaddress[min(addressed)]
                if any(byaddress[a].kind != first.kind for a in addressed):
                    raise Held("rodata", f"{name}: mixed private offset reference types")
                width = first.end - first.address
                if (
                    entry[0] > first.address
                    or (first.address - entry[0]) % width
                    or max(addressed) - entry[0] > 0x10000
                ):
                    raise Held("rodata", f"{name}: cannot represent its private offset references")
                offset_values = []
                for address in range(entry[0], max(addressed) + width, width):
                    element = byaddress.get(address)
                    offset_values.append(literal(reader(address, width), first.kind) if element is not None else "0.0")
                local = "private_" + first.kind + "_" + str(len(declarations))
                declarations[name] = (
                    f"static const {match['type'].removeprefix('const ')} {local}[] = {{{', '.join(offset_values)}}};\n"
                )
                addresses[name] = local
                substitutions[name] = local + "[0]"
                removed.append(match.span())
            continue
        if item.kind == "jump table":
            code = re.sub(r"/\*.*?\*/|//[^\n]*", "", source[: match.start()] + source[match.end() :], flags=re.S)
            if re.search(r"\b" + re.escape(name) + r"\b", code):
                source = lower(source, name, (item.end - item.address) // 4)
                # Source spans changed; restart declaration discovery.
                return rewrite(project, version, source, objects, references, reader)
        elif item.kind in ("float", "double", "string"):
            value = literal(reader(item.address, item.end - item.address), item.kind)
            if item.kind in ("float", "double") and re.search(r"\b" + re.escape(name) + r"\s*\.", code):
                local = "private_" + item.kind + "_" + str(len(declarations))
                declarations[name] = f"static const {match['type'].removeprefix('const ')} {local} = {{{value}}};\n"
                substitutions[name] = local
            elif item.kind in ("float", "double") and re.search(ADDRESS + re.escape(name) + r"\b", code):
                local = "private_" + item.kind + "_" + str(len(declarations))
                width = item.end - item.address
                entries = []
                address = item.address
                while address in byaddress and byaddress[address].kind == item.kind:
                    entries.append(literal(reader(address, width), item.kind))
                    address = byaddress[address].end
                declarations[name] = (
                    f"static const {match['type'].removeprefix('const ')} {local}[] = {{{', '.join(entries)}}};\n"
                )
                if not match["array"]:
                    addresses[name] = local
                substitutions[name] = local if match["array"] else local + "[0]"
            elif match["array"] and item.kind in ("float", "double"):
                width = item.end - item.address
                values = {}
                address = item.address
                while address in byaddress and byaddress[address].kind == item.kind:
                    element = byaddress[address]
                    values[(address - item.address) // width] = literal(reader(address, width), item.kind)
                    address = element.end
                arrays[name] = values
            elif item.kind == "string" and match["type"].removeprefix("const ") not in ("char", "s8", "u8"):
                local = "private_string_" + str(len(declarations))
                declarations[name] = f"static const {match['type'].removeprefix('const ')} {local} = {{{value}}};\n"
                substitutions[name] = local
            else:
                substitutions[name] = value
        else:
            continue
        removed.append(match.span())
    for start, end in reversed(removed):
        matched = DECLARATION.match(source, start)
        replacement = declarations.get(matched["name"], "") if matched is not None else ""
        source = source[:start] + replacement + source[end:]
    for name, values in arrays.items():
        source = re.sub(
            r"\b" + re.escape(name) + r"\s*\[\s*(0[xX][0-9a-fA-F]+|[0-9]+)\s*\]",
            partial(array_literal, values),
            source,
        )
        code = re.sub(r"/\*.*?\*/|//[^\n]*", "", source, flags=re.S)
        if re.search(r"\b" + re.escape(name) + r"\b", code):
            raise Held("rodata", f"{name}: private arrays require constant element indices")
    for name, local in addresses.items():
        source = re.sub(ADDRESS + re.escape(name) + r"\b", partial(replacement_text, local), source)
    for name, value in substitutions.items():
        if value.startswith('"'):
            source = re.sub(ADDRESS + re.escape(name) + r"\b", partial(replacement_text, value), source)
        source = re.sub(r"\b" + re.escape(name) + r"\b", partial(replacement_text, value), source)
    source = (
        re.sub(r"^.*jtbl_.*\n", "", source, flags=re.M)
        if not re.search(r"jtbl_", re.sub(r"/\*.*?\*/|//[^\n]*", "", source, flags=re.S))
        else source
    )
    return source


def carve(
    project: Project, version: str, function: split.Function, census: Census, objects: list[Constant], reader: RomReader
) -> split.Edit:
    configured = project.version(version)
    if any(path.is_symlink() for path in (configured.split, *configured.split.parents)):
        raise Held("rodata", f"{configured.split}: cannot edit through a symlink")
    locations = [reader.backing_row(o.address, o.end - o.address) for o in objects]
    row = locations[0][1]
    if any(other.line != row.line for _, other in locations):
        raise Held("rodata", f"{function.name}: constants cross resident rows")
    if row.kind != "bin":
        raise Held("rodata", f"{function.name}: constants are not in one resident bin row")
    start = min(offset for offset, _ in locations)
    stop = max(offset + o.end - o.address for (offset, _), o in zip(locations, objects, strict=False))
    stop = (stop + 3) & ~3
    if stop > split.end(row):
        raise Held("rodata", "pool alignment crosses its resident row")
    base = min(o.address for o in objects)
    if start % 4:
        raise Held("rodata", f"{function.name}: pool start is not word aligned")
    # Never absorb another owner's constant or a referenced unclassified interval.
    for item in census.objects:
        if (
            item.address < base + stop - start
            and item.end > base
            and (
                item.owners - {Path(function.path).stem}
                or item.writes
                or item.data_references
                or item.rom_pointer_candidates
            )
        ):
            raise Held("rodata", f"{function.name}: pool envelope overlaps nonprivate bytes at 0x{item.address:X}")
    before, lines, _ = split.layout(configured.split)
    indent = row.match["indent"]
    original_end = split.end(row)
    output = []
    # A copied runtime pool needs a distinct native segment when its original
    # enclosing text segment describes the pre-copy address.
    runtime = reader.span(base, stop - start)
    if split.address(row, configured.split) != runtime.address + row.start - runtime.offset:
        remaining = row.segment.rows[row.segment.rows.index(row) + 1 :]
        if any(other.kind != "bin" for other in remaining):
            raise Held("rodata", "runtime-address segment conversion requires trailing bin rows")
        if row.start != runtime.offset or original_end != runtime.offset + runtime.end - runtime.address:
            raise Held("rodata", "runtime copy does not cover the complete resident bin row")
        if row.segment.rows.index(row) == 0:
            header = max(i for i in range(row.line) if lines[i].startswith("  - "))
            for i in range(header, row.line):
                lines[i] = ""
        segment_name = f"constants_{runtime.address:08X}"
        output.extend(
            [
                f"  - name: {segment_name}\n",
                "    type: code\n",
                f"    start: 0x{row.start:X}\n",
                f"    vram: 0x{runtime.address:X}\n",
                "    subalign: 4\n",
                "    subsegments:\n",
            ]
        )
    if start > row.start:
        output.append(f"{indent}- [0x{row.start:X}, bin, {row.path}]\n")
    output.append(f"{indent}- [0x{start:X}, .rodata, {function.path}]\n")
    if stop < original_end:
        output.append(f"{indent}- [0x{stop:X}, bin, {Path(row.path).parent.as_posix()}/resident_{stop:08X}]\n")
    if split.address(row, configured.split) != runtime.address + row.start - runtime.offset and remaining:
        output.extend(
            [
                f"  - name: constants_continuation_{original_end:X}\n",
                "    type: code\n",
                f"    start: 0x{original_end:X}\n",
                f"    vram: 0x{split.address(row, configured.split) + original_end - row.start:X}\n",
                "    subalign: 4\n",
                "    subsegments:\n",
            ]
        )
    lines[row.line] = "".join(output)
    after = "".join(lines)
    # Native automatic selectors are per segment; an explicit pool in another
    # segment must remain the sole load selector for its compiler section.
    automatic = re.search(r"^  auto_link_sections:.*$", after, re.M)
    if automatic is None:
        after = after.replace("options:\n", 'options:\n  auto_link_sections: [".data", ".bss"]\n', 1)
    elif ".rodata" in automatic[0] or ".rdata" in automatic[0]:
        raise Held("rodata", "explicit auto_link_sections must exclude compiler constant sections")
    split.parse_layout(configured.split, after)
    return split.Edit(configured.split, before, after, (version,))


def migrate(project: Project, name: str, censuses: dict[str, Census] | None = None) -> list[split.Edit]:
    split.name(name)
    sourcepath = project.src / (name + ".c")
    # Submission queues evidence; match run publishes the C split rows. Check
    # that prerequisite before reading a draft or collecting object evidence.
    for version in project.versions:
        functions = censuses[version].functions if censuses is not None else split.functions(project, version)
        candidates = [f for f in functions if name in f.aliases or Path(f.path).stem == name]
        if candidates and (len(candidates) != 1 or candidates[0].kind != "c"):
            raise Held(
                "rodata",
                f"{name}: precondition published-C-row in VERSION {version}: required one C row; "
                f"run match submit {sourcepath}, then match run to publish, then rodata migrate {name}",
            )
    source = split.read(sourcepath)
    if any(path.is_symlink() for path in (sourcepath, *sourcepath.parents)):
        raise Held("rodata", f"{sourcepath}: cannot edit through a symlink")
    edits: list[split.Edit] = []
    variants: dict[str, list[str]] = {}
    present = []
    for version in project.versions:
        census = censuses[version] if censuses is not None else scan(project, version)
        candidates = [f for f in census.functions if name in f.aliases or Path(f.path).stem == name]
        if not candidates:
            continue
        function = candidates[0]
        objects = private(census, Path(function.path).stem)
        if not objects:
            raise Held("rodata", f"{name}: no private typed constants in VERSION {version}")
        if any(error.startswith(Path(function.path).stem + ":") for error in census.errors):
            raise Held("rodata", f"{name}: incomplete relocation evidence in VERSION {version}")
        references = [r for r in census.references if r.owner == Path(function.path).stem]
        original = project.build_link(version) / "obj/src" / (function.path + ".o")
        if original.is_file():
            image = project.version(version).baserom.read_bytes()
            extra, failures = collect(
                Path(function.path).stem, image[function.start : function.end], Object(original), None
            )
            if failures:
                raise Held(
                    "rodata", f"{name}: incomplete source relocation evidence in VERSION {version}: {failures[0]}"
                )
            references.extend(extra)
        reader = project_reader(project, version)
        transformed = rewrite(project, version, source, objects, references, reader)
        edits.append(carve(project, version, function, census, objects, reader))
        variants.setdefault(transformed, []).append(version)
        present.append(version)
    if not present:
        raise Held("rodata", f"{name}: absent from every VERSION")
    if len(variants) == 1:
        after = next(iter(variants))
    else:
        blocks = []
        for index, (text, versions) in enumerate(variants.items()):
            macros = []
            for version in versions:
                defined = [
                    m
                    for m in project.version(version).macros
                    if re.fullmatch(r"[A-Za-z_]\w*", m)
                    and all(m not in project.version(other).macros for other in project.versions if other != version)
                ]
                if len(defined) != 1:
                    raise Held("rodata", f"{version}: required one VERSION macro for differing constants")
                macros.append(f"defined({defined[0]})")
            blocks.append(("#if " if index == 0 else "#elif ") + " || ".join(macros) + "\n" + text)
        after = "".join(blocks) + "#endif\n"
    edits.append(split.Edit(sourcepath, source, after, tuple(present)))
    return edits
