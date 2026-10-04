"""Prepare a complete split unit and header-backed declarations for m2c."""

from __future__ import annotations

import hashlib
import json
import math
import re
import struct
from pathlib import Path

from unbake.config import Held, Project
from unbake.extract import discovered_symbols
from unbake.layout import split


def assembly_source(project: Project, version: str, function: str, extracted: Path) -> tuple[str, int]:
    """The function's split row assembly from the extraction, and its address."""
    rows = [row for row in split.functions(project, version) if function in row.aliases]
    if len(rows) != 1:
        raise Held("m2c", f"{function}: expected one function row in VERSION {version}, found {len(rows)}")
    row = rows[0]
    path = extracted / "asm" / (row.path + ".s")
    if not path.is_file():
        raise Held("m2c", f"{function}: extraction has no assembly for row {row.path} in VERSION {version}")
    return path.read_text(), row.address


def canonical_entry(
    project: Project, version: str, function: str, address: int, assembly: str, *, generation: Path | None = None
) -> str:
    """Use the build's discovered symbol placements to name the entry."""
    labels = re.findall(r"^\s*glabel\s+(\S+)\s*$", assembly, re.M)
    if function in labels or re.search(rf"^\s*{re.escape(function)}:\s*$", assembly, re.M):
        return assembly
    if generation is None:
        raise Held("m2c", f"{function}: extraction directory required")
    dump = generation / "splat_symbols.csv"
    try:
        values = discovered_symbols(dump, {})
    except (OSError, ValueError, KeyError) as error:
        raise Held("m2c", f"{dump}: entry correspondence for {function}: {error}") from error
    entries = [name for name in labels if values.get(name) == address]
    if len(entries) != 1:
        raise Held("m2c", f"{function}: expected one entry label at 0x{address:08X}, found {len(entries)}")
    return re.sub(rf"(?<![\w.$]){re.escape(entries[0])}(?![\w.$])", function, assembly)


def version_for(project: Project, version: str | None, phase: str) -> str:
    """Infer only a uniquely configured VERSION; name ambiguous choices."""
    if version is None:
        if len(project.versions) != 1:
            raise Held(phase, f"--version is ambiguous; choose from {', '.join(project.versions)}")
        version = project.versions[0]
    project.version(version)
    return version


def header_types(output: str, context: str) -> str:
    """Use project typedefs where m2c repeats their declarations."""
    typedef = re.compile(r"\btypedef\s+[^;{}]+?\b([A-Za-z_]\w*)\s*;\s*")
    names = {match[1] for match in typedef.finditer(context)}
    return typedef.sub(lambda match: "" if match[1] in names else match[0], output)


def whole_body(assembly: str, function: str) -> str:
    """Internal global labels in one split unit belong to the selected body."""
    labels = re.findall(r"^\s*glabel\s+(\S+)\s*$", assembly, re.M)
    if function not in labels:
        return assembly
    internal = {name: ".L_" + name for name in labels if name != function}
    for name, local in internal.items():
        assembly = re.sub(rf"(?<![\w.$]){re.escape(name)}(?![\w.$])", local, assembly)
    lines = []
    for line in assembly.splitlines():
        label = re.fullmatch(r"\s*glabel\s+(\S+)\s*", line)
        if label and label[1] in internal.values():
            line = label[1] + ":"
        if re.match(r"\s*(?:endlabel|nonmatching)\b", line):
            continue
        lines.append(line)
    return "\n".join(lines) + "\n"


def jump_tables(project: Project, version: str, function: str, assembly: str) -> str:
    """Supply ROM-backed local jump tables omitted by text-only extraction."""
    from unbake.decomp.rom import function_span, project_reader, symbol_values

    names = set(re.findall(r"%hi\((jtbl_[A-Za-z0-9_]+)\)", assembly))
    if not names:
        return assembly
    configured = project.version(version)
    values = symbol_values(configured.symbols)
    span = function_span(configured, function, values)
    if span is None:
        raise Held("m2c", f"{function}: missing split placement in VERSION {version}")
    read_memory = project_reader(project, version)
    tables = []
    targets = set()
    for name in sorted(names):
        if re.search(rf"^\s*(?:glabel\s+{re.escape(name)}|{re.escape(name)}:)\s*$", assembly, re.M):
            continue
        address = values.get(name)
        if address is None:
            generated = re.fullmatch(r"jtbl_([0-9A-Fa-f]{8})", name)
            if generated is None:
                raise Held("m2c", f"{name}: jump table address is missing")
            address = int(generated[1], 16)
        entries = []
        # A table entry must identify an instruction in the complete split unit.
        mapping = read_memory.span(address, 4)
        for offset in range(0, min(span.size * 4, mapping.end - address), 4):
            target = read_memory.table_entry(address + offset)
            if not span.address <= target < span.address + span.size or target % 4:
                break
            entries.append(f".word .L{target:08X}")
            targets.add(target)
        if not entries:
            raise Held("m2c", f"{name}: no local jump table entries at 0x{address:08X}")
        tables.append(f"glabel {name}\n" + "\n".join(entries))
    for target in sorted(targets):
        label = f".L{target:08X}"
        if re.search(rf"^\s*{re.escape(label)}:\s*$", assembly, re.M):
            continue
        instruction = re.compile(rf"^(\s*/\*\s*[0-9A-Fa-f]+\s+{target:08X}\s+[^\n]+)$", re.M | re.I)

        def add_label(match: re.Match[str], label: str = label) -> str:
            return label + ":\n" + match[0]

        assembly, count = instruction.subn(add_label, assembly)
        if count != 1:
            raise Held("m2c", f"{function}: jump table target 0x{target:08X} requires one instruction, found {count}")
    return assembly + ("\n.section .rodata\n" + "\n".join(tables) + "\n" if tables else "")


def private_constants(
    project: Project, version: str, function: str, assembly: str, *, generation: Path | None = None
) -> str:
    """Expose only byte-pinned, sole-owner immutable literals to m2c."""
    manifest = project.build / "setup" / (version + ".json")
    if not manifest.is_file():
        return assembly
    try:
        providers = json.loads(manifest.read_bytes())["providers"]
        configured = project.version(version)
        from unbake.decomp.rom import symbol_values

        values = symbol_values(configured.symbols)
        if generation is None:
            raise Held("m2c", f"{function}: extraction directory required")
        build = generation
        symbols = build / "splat_symbols.csv"
        if symbols.is_file():
            discovered = discovered_symbols(symbols, {})
            for name, value in discovered.items():
                if name in values and values[name] != value:
                    raise ValueError(f"conflicting symbol {name}")
                values[name] = value
        addresses = build / "symbol-addresses.txt"
        if addresses.is_file():
            for line in addresses.read_text().splitlines():
                name, address, *_ = line.split()
                value = int(address, 0)
                if name in values and values[name] != value:
                    raise ValueError(f"conflicting symbol {name}")
                values[name] = value
        referenced = set(re.findall(r"%hi\(([A-Za-z_]\w*)\)", assembly))
        additions = []
        with configured.baserom.open("rb") as rom:
            for row in providers:
                evidence = row.get("evidence", {})
                kind = evidence.get("kind")
                if (
                    row.get("kind") != "private"
                    or row.get("owners") != [function]
                    or not evidence.get("safe_sole_candidate")
                    or evidence.get("writes")
                    or kind not in ("float", "double", "string")
                ):
                    continue
                names = sorted(name for name in referenced if values.get(name) == row.get("address"))
                names = [
                    name for name in names if not re.search(rf"^\s*(?:glabel\s+{name}|{name}:)\s*$", assembly, re.M)
                ]
                if not names:
                    continue
                start, end = row["start"], row["end"]
                if not isinstance(start, int) or not isinstance(end, int) or not 0 <= start < end:
                    raise ValueError("invalid private ROM span")
                rom.seek(start)
                data = rom.read(end - start)
                if len(data) != end - start or hashlib.sha256(data).hexdigest() != evidence["sha256"]:
                    raise ValueError(f"private bytes changed: {names[0]}")
                if kind == "string":
                    if not data.endswith(b"\0") or b"\0" in data[:-1]:
                        raise ValueError(f"invalid string span: {names[0]}")
                    directive = ".asciz " + json.dumps(data[:-1].decode("ascii"))
                else:
                    width, code = (4, "f") if kind == "float" else (8, "d")
                    if len(data) % width:
                        raise ValueError(f"invalid {kind} span: {names[0]}")
                    numbers = struct.unpack(">" + code * (len(data) // width), data)
                    if not all(math.isfinite(number) for number in numbers):
                        raise ValueError(f"nonfinite {kind} literal: {names[0]}")
                    directive = "." + kind + " " + ", ".join(repr(number) for number in numbers)
                additions.append("\n".join("glabel " + name for name in names) + "\n" + directive)
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise Held("m2c", f"draft.private_constants: {manifest}: {error}") from error
    return assembly + ("\n.section .rodata\n" + "\n".join(additions) + "\n" if additions else "")


def stack_locals(output: str, context: str, function: str, assembly: str = "") -> str:
    """Retain m2c's inferred types for stack loads that have no preceding store."""
    from unbake.layout.structs import layouts

    if "_m2c_stack_" + function not in output:
        return output
    prefix = context + "\n"
    template = next((record for record in layouts(prefix + output) if record.name == "_m2c_stack_" + function), None)
    if template is None:
        return output
    from unbake.decomp.draft_stack import overlay

    measured = overlay(output, template, len(prefix), function, assembly)
    if measured is not None:
        return measured
    start, end = template.start - len(prefix), template.end - len(prefix)
    trailing = re.match(r"\s*;", output[end:])
    if trailing:
        end += trailing.end()
    output = output[:start] + output[end:]
    entry = re.search(rf"\b{re.escape(function)}\s*\([^;{{}}]*\)\s*{{", output)
    if entry is None:
        raise Held("m2c", f"{function}: missing body for inferred stack declarations")
    body = output[entry.end() :]
    locals_text = body.split("\n\n", 1)[0]
    declared = set(
        re.findall(
            r"^[ \t]*(?!(?:return|goto|break|continue)\b)(?:[A-Za-z_]\w*[ \t]+)+"
            r"\**[ \t]*([A-Za-z_]\w*)\s*(?:\[[^\]]*\]\s*)*;",
            locals_text,
            re.M,
        )
    )
    missing = [
        field.declaration.strip()
        for field in template.fields
        if field.name not in declared
        and re.search(rf"\b{re.escape(field.name)}\b", body)
        and not field.name.startswith("pad")
    ]
    if missing:
        declarations = "\n" + "\n".join("    " + declaration for declaration in missing)
        output = output[: entry.end()] + declarations + output[entry.end() :]
    return output


def canonical_aliases(project: Project, version: str, assembly: str, generation: Path | None = None) -> str:
    """Resolve aliases from configured and extracted symbol addresses."""
    from unbake.decomp.draft_asm import address_aliases
    from unbake.decomp.rom import symbol_values

    values = symbol_values(project.version(version).symbols)
    if generation is None:
        raise Held("m2c", "canonical aliases: extraction directory required")
    build = generation
    dump = build / "splat_symbols.csv"
    if dump.is_file():
        for name, value in discovered_symbols(dump, {}).items():
            if name in values and values[name] != value:
                raise Held("m2c", f"{name}: conflicting symbol addresses")
            values[name] = value
    return address_aliases(assembly, values)
