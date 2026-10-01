"""Express inferred offset accesses through validated shared declarations."""

import hashlib
import re
from pathlib import Path

from unbake.layout import shared, split_apply
from unbake.layout.structs import layouts
from unbake.layout.structs_fold import fold
from unbake.project.config import Held, Project

_EXPRESSION = r"(?:[^(),]|\([^()]*\)|\((?:[^()]|\([^()]*\))*\))+?"
_FIELD = re.compile(rf"M2C_FIELD\(\s*({_EXPRESSION})\s*,\s*([^,()]+\*)\s*,\s*(0[xX][\da-fA-F]+|\d+)\s*\)")


def _base_name(base: str) -> str:
    base = base.strip()
    if re.fullmatch(r"[A-Za-z_]\w*", base):
        return base
    return "expression_" + hashlib.sha256(base.encode()).hexdigest()[:12]


def share(project: Project, function: str, output: str, context: str) -> tuple[str, Path | None]:
    """Create shared declarations only for explicit, nonoverlapping typed fields."""

    def arithmetic_load(match: re.Match[str]) -> str:
        base, pointer, literal = match.groups()
        if not re.fullmatch(r"[A-Za-z_]\w*", base.strip()):
            return f"*({pointer.strip()})((char *)({base}) + {literal})"
        return match[0]

    # An arithmetic address does not establish an aggregate object identity.
    output = _FIELD.sub(arithmetic_load, output)
    groups: dict[str, dict[int, str]] = {}
    for match in _FIELD.finditer(output):
        base, pointer, literal = match.groups()
        offset = int(literal, 0)
        type_name = pointer.strip()[:-1].strip()
        fields = groups.setdefault(_base_name(base), {})
        if offset in fields and fields[offset] != type_name:
            raise Held("m2c", f"{base}+{literal}: conflicting field types")
        fields[offset] = type_name
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
    names = {f"Layout_{function}_{base}" for base in groups}
    for record in reversed(layouts(context)):
        if record.name in names:
            context = context[: record.start] + context[record.end :]
    text = "\n\n".join(declarations) + "\n"
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

    def replace(match: re.Match[str]) -> str:
        base, _pointer, literal = match.groups()
        return f"((struct Layout_{function}_{_base_name(base)} *)({base}))->field_{int(literal, 0):X}"

    return _FIELD.sub(replace, output), header
