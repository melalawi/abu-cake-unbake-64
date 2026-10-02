"""Lower offset accesses using existing declarations without creating local types."""

import re
from pathlib import Path
from typing import Any

from unbake.decomp.draft_macros import calls
from unbake.layout.structs_parser import Parser
from unbake.layout.structs_types import SCALARS
from unbake.project.config import Held, Project


def share(
    project: Project,
    function: str,
    output: str,
    context: str,
    *,
    layouts: dict[str, Any] | None = None,
) -> tuple[str, Path | None]:
    """Use a declared base type or preserve a typed byte-offset access.

    An offset and a local variable name never establish aggregate identity.
    This is a pure transform: shared declarations belong to the project solve.
    """
    parser = Parser(context)
    try:
        records = parser.parse()
    except Held:
        records = []
    bases = {}
    for record in records:
        for spelling in (f"{record.kind} {record.name}", *record.aliases):
            pattern = rf"\b{re.escape(spelling)}\s*\*\s*([A-Za-z_]\w*)\b"
            for match in re.finditer(pattern, output):
                bases[match[1]] = record
    observed = {}
    nested = {}
    available = {record.name for record in records}
    parameters = {}
    if layouts:
        from unbake.typemap import declarations

        # The body still contains macro arguments spelled as C types, which
        # are not expressions. Only the definition's declarator is needed.
        definition = re.search(rf"\b{re.escape(function)}\s*\(([^{{}}]*?)\)\s*{{", output)
        try:
            signature = (
                declarations.extract(context + f"\nvoid {function}({definition[1]});", {})["functions"].get(function)
                if definition is not None
                else None
            )
        except Held:
            signature = None
        if signature is not None:
            parameters = {
                f"param:{function}:{register}": param["name"]
                for param, register in zip(signature["params"], signature["registers"], strict=True)
                if register is not None
            }
    for name, layout in (layouts or {}).items():
        if layout["state"] != "known" or name not in available or function not in layout.get("users", []):
            continue
        nested[layout.get("common_base", "")] = layout
        for origin in layout.get("base_nodes", [layout.get("common_base", "")]):
            if origin.startswith("global:"):
                observed[origin.removeprefix("global:")] = layout
            elif origin.startswith("address:"):
                observed["&" + origin.removeprefix("address:")] = layout
            elif origin in parameters:
                observed[parameters[origin]] = layout

    # One assignment of an unchanged source preserves identity. Reassignment,
    # arithmetic and coincident offsets never establish a common object.
    assignments = re.findall(r"\b([A-Za-z_]\w*)\s*=\s*(&?[A-Za-z_]\w*)\s*;", output)
    writes = re.findall(r"\b([A-Za-z_]\w*)\s*(?:=(?!=)|[+*/&|^-]=|\+\+|--)", output)
    for local, source in assignments:
        if writes.count(local) == 1 and source in observed:
            observed[local] = observed[source]

    def replace(args: list[str]) -> str:
        if len(args) != 3:
            raise Held("m2c", "unresolved M2C_FIELD(" + ", ".join(args) + ")")
        base, pointer, literal = (value.strip() for value in args)
        if not pointer.endswith("*") or not re.fullmatch(r"[+-]?(?:0[xX][\da-fA-F]+|\d+)", literal):
            raise Held("m2c", "unresolved M2C_FIELD(" + ", ".join(args) + ")")
        offset = int(literal, 0)
        scalar = Parser(pointer[:-1].strip() + " measured;")
        scalar.types = parser.types.copy()
        try:
            member = scalar.declaration()[0]
            type_name = scalar.type_name(member.base, member.operations)
        except Held:
            type_name = pointer[:-1].strip()
        width = 4 if type_name.endswith(" *") else SCALARS.get(type_name, (None, None))[0]
        layout = observed.get(base)
        if layout is not None:
            fields = [
                field for field in layout.get("fields", []) if field["offset"] == offset and field["size"] == width
            ]
            if len(fields) == 1:
                field = fields[0]
                view = f"(({layout['type']} *)({base}))->{field['name']}"
                if field["state"] == "known":
                    lowered = view if field["type"] == type_name else f"*({pointer})(&({view}))"
                else:
                    lowered = f"*({pointer})({view})"
                child = nested.get(f"field:{layout['common_base']}:{offset}")
                if child is not None:
                    observed[lowered] = child
                return lowered
        record = bases.get(base)
        if record is not None:
            members = [
                member
                for member in record.fields
                if member.offset == offset
                and member.type == type_name
                and not member.extent
                and not member.fields
                and member.bit_size is None
            ]
            if len(members) == 1:
                return f"({base})->{members[0].name}"
        return f"*({pointer})((char *)({base}) + ({literal}))"

    return calls(output, "M2C_FIELD", replace), None
