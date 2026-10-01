"""Lower offset accesses using existing declarations without creating local types."""

import re
from pathlib import Path

from unbake.decomp.draft_macros import calls
from unbake.layout.structs_parser import Parser
from unbake.project.config import Held, Project


def share(project: Project, function: str, output: str, context: str) -> tuple[str, Path | None]:
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

    def replace(args: list[str]) -> str:
        if len(args) != 3:
            raise Held("m2c", "unresolved M2C_FIELD(" + ", ".join(args) + ")")
        base, pointer, literal = (value.strip() for value in args)
        if not pointer.endswith("*") or not re.fullmatch(r"[+-]?(?:0[xX][\da-fA-F]+|\d+)", literal):
            raise Held("m2c", "unresolved M2C_FIELD(" + ", ".join(args) + ")")
        offset = int(literal, 0)
        record = bases.get(base)
        if record is not None:
            scalar = Parser(pointer[:-1].strip() + " measured;")
            scalar.types = parser.types.copy()
            try:
                member = scalar.declaration()[0]
                type_name = scalar.type_name(member.base, member.operations)
            except Held:
                type_name = pointer[:-1].strip()
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
