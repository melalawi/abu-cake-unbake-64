"""Prepare a complete split unit and header-backed declarations for m2c."""

from __future__ import annotations

import re

from unbake.project.config import Held, Project


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
    from unbake.decomp.trial_layout import function_span, project_reader, symbol_values

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
