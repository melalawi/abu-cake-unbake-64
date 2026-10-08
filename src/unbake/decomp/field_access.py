"""Lower offset accesses using existing declarations without creating local types."""

import hashlib
import re
from pathlib import Path

from pycparser import c_ast  # type: ignore[import-untyped]

from unbake import atomic as atomic_files
from unbake import cdecl
from unbake.cdecl import LayoutParser
from unbake.config import Held, Project
from unbake.decomp.draft_macros import calls
from unbake.layout.structs_types import SCALARS
from unbake.process import capture
from unbake.process import named as cause_named


def share(
    project: Project,
    function: str,
    output: str,
    context: str,
    *,
    types_path: Path | None = None,
) -> tuple[str, Path | None]:
    """Use a declared base type or publish a measured storage view.

    An offset and a local variable name never establish aggregate identity.
    Fallback views describe only the measured access, never aggregate identity.
    """
    parser = LayoutParser(context)
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
    from unbake.typemap import types_db

    layouts = types_db.entries(types_path, "structs", available) if types_path is not None else {}
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
    for layout in layouts.values():
        if layout["state"] != "known" or function not in layout.get("users", []):
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

    views: dict[str, str] = {}
    from unbake.typemap.declarations import declarator, node_type

    def replace(args: list[str]) -> str:
        if len(args) != 3:
            raise Held(
                cause_named(
                    "decomp.field_access.replace",
                    "unresolved M2C_FIELD(" + ", ".join(args) + ")",
                    owner="decomp.field_access",
                    stage="m2c",
                )
            )
        base, pointer, literal = (value.strip() for value in args)
        if not re.fullmatch(r"[+-]?(?:0[xX][\da-fA-F]+|\d+)", literal):
            raise Held(
                cause_named(
                    "decomp.field_access.replace",
                    "unresolved M2C_FIELD(" + ", ".join(args) + ")",
                    owner="decomp.field_access",
                    stage="m2c",
                )
            )
        offset = int(literal, 0)
        declaration = declarator(pointer, "measured") + ";"
        scalar = LayoutParser(declaration)
        scalar.types = parser.types  # a measured declaration defines no type
        try:
            member = scalar.declaration()[0]
            if not member.operations or member.operations[0][0] != "pointer":
                raise Held(
                    cause_named(
                        "decomp.field_access.replace",
                        "field type requires an outer pointer",
                        owner="decomp.field_access",
                        stage="m2c",
                    )
                )
            type_name = scalar.type_name(member.base, member.operations[1:])
        except Held as error:
            raise Held(
                capture(
                    error,
                    cause=cause_named(
                        "decomp.field_access.replace",
                        "unresolved M2C_FIELD(" + ", ".join(args) + ")",
                        owner="decomp.field_access",
                        stage="m2c",
                    ),
                )
            ) from error
        record_type = type_name
        width = 4 if type_name.endswith(" *") else SCALARS.get(type_name, (None, None))[0]
        alignment = 4 if type_name.endswith(" *") else SCALARS.get(type_name, (None, None))[1]
        if "(" in type_name:
            # The macro supplies a pointer to its lvalue. A callback lvalue
            # is itself a pointer, whose target ABI width is known without
            # changing or guessing its return and parameter declarations.
            try:
                value = cdecl.parse(declaration, typedefs=parser.types).ext[0].type
                if not isinstance(value, c_ast.PtrDecl) or not isinstance(value.type, c_ast.PtrDecl):
                    raise Held(
                        cause_named(
                            "decomp.field_access.replace",
                            "field value is not a pointer",
                            owner="decomp.field_access",
                            stage="m2c",
                        )
                    )
                type_name = node_type(value.type)
                width, alignment = 4, 4
            except (cdecl.ParseError, Held) as error:
                raise Held(
                    capture(
                        error,
                        cause=cause_named(
                            "decomp.field_access.replace",
                            "unresolved M2C_FIELD(" + ", ".join(args) + ")",
                            owner="decomp.field_access",
                            stage="m2c",
                        ),
                    )
                ) from error
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
                and member.type == record_type
                and not member.extent
                and not member.fields
                and member.bit_size is None
            ]
            if len(members) == 1:
                return f"({base})->{members[0].name}"
        if width is None or width <= 0:
            raise Held(
                cause_named(
                    f"{function}",
                    f"{function}: field at {literal} has no measured scalar width: {pointer}",
                    owner="decomp.field_access",
                    stage="m2c",
                )
            )
        if alignment is None or offset % alignment:
            raise Held(
                cause_named(
                    f"{function}",
                    f"{function}: unaligned field at {literal}: {pointer}",
                    owner="decomp.field_access",
                    stage="m2c",
                )
            )
        tag, access, view = storage_view(function, base, offset, type_name, width, alignment, literal)
        views[tag] = view
        return access

    output = calls(output, "M2C_FIELD", replace)
    if not views:
        return output, None
    shared = project.include[0] / "common" / f"draft_fields_{function}.h"
    atomic_files.text(shared, header_text(function, list(views.values())))
    return output, shared


def header_text(function: str, views: list[str]) -> str:
    guard = f"UNBAKE_DRAFT_FIELDS_{function.upper()}_H"
    return f"#ifndef {guard}\n#define {guard}\n" + "\n".join(views) + "\n#endif\n"


def storage_view(
    function: str, base: str, offset: int, type_name: str, width: int, alignment: int, literal: str
) -> tuple[str, str, str]:
    """Tag, access expression and declaration of one measured storage view."""
    from unbake.typemap.declarations import declarator

    key = hashlib.sha256(f"{offset}:{type_name}".encode()).hexdigest()[:12]
    tag = f"Measured_{function}_{key}"
    if offset >= 0:
        padding = f"unsigned char padding[{offset}]; " if offset else ""
        body = padding + declarator(type_name, "value") + ";"
        access = f"((struct {tag} *)({base}))->value"
    else:
        size = max(-offset, width)
        size = (size + alignment - 1) // alignment * alignment
        prefix_size = size + offset
        body = (f"unsigned char padding[{prefix_size}]; " if prefix_size else "") + declarator(type_name, "value") + ";"
        trailing = size - prefix_size - width
        if trailing:
            body += f" unsigned char trailing[{trailing}];"
        access = f"((struct {tag} *)({base}))[-1].value"
    view = (
        f"/* Measured {width}-byte access at {literal}; storage view, complete object unknown. */\n"
        f"struct {tag} {{ {body} }};"
    )
    return tag, access, view


def restore(project: Project, function: str, source: str) -> Path | None:
    """Rebuild the draft's measured-storage header when a source includes it and no copy exists.

    A view tag is a hash of its offset and field type, so each tag the source uses is recovered by searching
    the scalar types and the pointer types the source names. A tag that cannot be recovered is refused."""
    name = f"common/draft_fields_{function}.h"
    if not re.search(rf'^[ \t]*#[ \t]*include[ \t]*"{re.escape(name)}"', source, re.M):
        return None
    for root in (*project.work_include, project.work / function / "include", *project.include):
        if (root / name).is_file():
            return None
    wanted = list(dict.fromkeys(re.findall(rf"\bstruct\s+Measured_{re.escape(function)}_([0-9a-f]{{12}})\b", source)))
    pointers = {"void *"}
    for spelled in re.findall(r"\b((?:struct\s+)?[A-Za-z_]\w*)\s*\*", source):
        pointers.add(f"{spelled} *")
    types = {**SCALARS, **{t: (4, 4) for t in sorted(pointers)}}
    found: dict[str, str] = {}
    for offset in range(-256, 0x20000):
        for type_name, (width, alignment) in types.items():
            if offset % alignment:
                continue
            key = hashlib.sha256(f"{offset}:{type_name}".encode()).hexdigest()[:12]
            if key in wanted and key not in found:
                literal = f"{offset:#x}".upper().replace("0X", "0x") if offset > 9 else str(offset)
                found[key] = storage_view(function, "base", offset, type_name, width, alignment, literal)[2]
        if wanted and len(found) == len(wanted):
            break
    missing = [key for key in wanted if key not in found]
    if missing or not wanted:
        raise Held(
            cause_named(
                "draft.fields_missing",
                f"draft.fields_missing: {function}: {name} is included but absent from every include root; "
                + (f"measured views {missing} cannot be rebuilt" if wanted else "the source uses no measured view"),
                owner="decomp.field_access",
                stage="draft",
            )
        )
    shared = project.work / function / "include" / name
    atomic_files.text(shared, header_text(function, [found[key] for key in wanted]))
    return shared
