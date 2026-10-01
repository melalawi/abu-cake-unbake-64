"""Express inferred offset accesses through validated shared declarations."""

import hashlib
import re
from pathlib import Path

from unbake.decomp.draft_macros import calls
from unbake.layout import shared, split_apply
from unbake.layout.structs import layouts
from unbake.layout.structs_fold import fold
from unbake.layout.structs_parser import Parser
from unbake.project.config import Held, Project


def _base_name(base: str) -> str:
    base = base.strip()
    if re.fullmatch(r"[A-Za-z_]\w*", base):
        return base
    return "expression_" + hashlib.sha256(base.encode()).hexdigest()[:12]


def share(project: Project, function: str, output: str, context: str) -> tuple[str, Path | None]:
    """Create shared declarations only for explicit, nonoverlapping typed fields."""

    groups: dict[str, dict[int, str]] = {}

    def collect(args: list[str]) -> str:
        if len(args) != 3:
            raise Held("m2c", "unresolved M2C_FIELD(" + ", ".join(args) + ")")
        base, pointer, literal = args
        if not pointer.endswith("*") or not re.fullmatch(r"[+-]?(?:0[xX][\da-fA-F]+|\d+)", literal):
            raise Held("m2c", "unresolved M2C_FIELD(" + ", ".join(args) + ")")
        offset = int(literal, 0)
        if offset < 0 or not re.fullmatch(r"[A-Za-z_]\w*", base):
            return f"*({pointer})((char *)({base}) + ({literal}))"
        type_name = pointer[:-1].strip()
        fields = groups.setdefault(_base_name(base), {})
        if offset in fields and fields[offset] != type_name:
            # Multiple typed views do not establish one aggregate layout.
            return f"*({pointer})((char *)({base}) + ({literal}))"
        fields[offset] = type_name
        return f"M2C_FIELD({base}, {pointer}, {literal})"

    output = calls(output, "M2C_FIELD", collect)
    if not groups:
        return output, None
    declarations = []
    for base, fields in groups.items():
        cursor = 0
        members = []
        for offset, type_name in sorted(fields.items()):
            if offset < cursor:
                raise Held("m2c", f"{base}+0x{offset:X}: overlapping field")
            if offset > cursor:
                members.append(f"char padding_{cursor:X}[0x{offset - cursor:X}];")
            declaration = f"{type_name} field_{offset:X};"
            member = layouts(context + "\nstruct MeasuredField { " + declaration + " };")[-1].fields[0]
            if offset % member.size and not type_name.endswith("*"):
                raise Held("m2c", f"{base}+0x{offset:X}: unaligned {type_name} field")
            members.append(declaration)
            cursor = offset + member.size
        declarations.append(f"struct Layout_{function}_{base} {{\n    " + "\n    ".join(members) + "\n};")
    existing = {
        record.name: record
        for record in Parser(
            "\n".join(path.read_text() for root in project.include for path in sorted(Path(root).rglob("*.h")))
        ).parse()
    }
    names_by_base: dict[str, str] = {}
    for base, declaration in zip(groups, declarations, strict=True):
        name = f"Layout_{function}_{base}"
        previous = existing.get(name)
        if previous is not None:
            old = {member.name: (member.offset, member.type, member.size) for member in previous.fields}
            measured_record = layouts(context + "\n" + declaration.replace(name, "MeasuredLayout", 1))[-1]
            if any(
                member.name in old and old[member.name] != (member.offset, member.type, member.size)
                for member in measured_record.fields
                if member.name.startswith("field_")
            ):
                name += "_" + hashlib.sha256(declaration.encode()).hexdigest()[:12]
        names_by_base[base] = name
    for record in reversed(layouts(context)):
        if record.name in set(names_by_base.values()):
            context = context[: record.start] + context[record.end :]
    text = (
        "\n\n".join(
            declaration.replace(f"Layout_{function}_{base}", names_by_base[base], 1)
            for base, declaration in zip(groups, declarations, strict=True)
        )
        + "\n"
    )
    records = layouts(context + "\n" + text)[-len(groups) :]
    for record, fields in zip(records, groups.values(), strict=True):
        measured = {member.name: member.offset for member in record.fields}
        for offset in fields:
            if measured[f"field_{offset:X}"] != offset:
                raise Held("m2c", f"{record.name}.field_{offset:X}: layout does not preserve offset")
    header = shared.home(project)
    for edit in fold(records, project):
        if edit.path.is_symlink():
            raise Held("m2c", f"{edit.path}: shared header must not be a symlink")
        split_apply.write(edit.path, edit.after)

    def replace(args: list[str]) -> str:
        base, _pointer, literal = args
        return f"((struct {names_by_base[_base_name(base)]} *)({base}))->field_{int(literal, 0):X}"

    return calls(output, "M2C_FIELD", replace), header
