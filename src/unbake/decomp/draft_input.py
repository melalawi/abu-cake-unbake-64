"""Prepare a complete split unit and header-backed declarations for m2c."""

from __future__ import annotations

import re
import shlex
from pathlib import Path

from unbake.layout import split
from unbake.project.config import Held, Project
from unbake.project_tools.extract import discovered_symbols


def assembly_source(project: Project, version: str, function: str) -> tuple[Path, int]:
    """Select the current split unit, excluding stale nonmatching copies."""
    rows = [row for row in split.functions(project, version) if function in row.aliases]
    if len(rows) != 1:
        raise Held("m2c", f"{function}: expected one function row in VERSION {version}, found {len(rows)}")
    row = rows[0]
    root = project.asm / version
    if row.kind == "asm":
        path = root / (row.path + ".s")
    else:
        directory = root / "nonmatchings" / row.path
        paths = sorted(directory.rglob(function + ".s"))
        if len(paths) != 1:
            raise Held("m2c", f"{directory}: expected one current assembly file {function}.s, found {len(paths)}")
        path = paths[0]
    if not path.is_file():
        command = shlex.join(["make", "-C", str(project.root), f"VERSION={version}", "extract"])
        raise Held("m2c", f"{path}: current assembly source is missing; generate: {command}")
    return path, row.address


def canonical_entry(project: Project, version: str, function: str, address: int, assembly: str) -> str:
    """Use the build's discovered symbol placements to name the entry."""
    labels = re.findall(r"^\s*glabel\s+(\S+)\s*$", assembly, re.M)
    if function in labels or re.search(rf"^\s*{re.escape(function)}:\s*$", assembly, re.M):
        return assembly
    dump = project.build_link(version) / "splat_symbols.csv"
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
