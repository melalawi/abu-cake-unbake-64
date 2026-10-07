"""Compare explicit C layouts to a solved O32 register signature."""

from __future__ import annotations

from typing import Any

from unbake import cdecl
from unbake.config import Held
from unbake.typemap import declarations


def equivalent(source: str, signature: dict[str, Any], machine: dict[str, Any], aliases: dict[str, str]) -> bool:
    """Validate the supplied aggregate grouping without inferring a C grouping."""
    if machine.get("state") != "known" or not signature.get("arity_known"):
        return False
    layouts = {f"{layout.kind} {layout.name}": layout for layout in cdecl.records(source)}
    slots: list[dict[str, str]] = []
    slot = 0
    floating_prefix = True

    def add(type_: str, index: int, floating: str | None = None) -> None:
        reg = floating or (f"r{4 + index}" if index < 4 else f"stack{index * 4}")
        slots.append({"register": reg, "type": type_})

    for i, parameter in enumerate(signature["params"]):
        type_ = declarations.canonical(parameter["type"], aliases)
        if type_ in layouts:
            layout = layouts[type_]
            # Union alternatives and padding do not establish word semantics.
            if layout.kind != "struct" or not layout.size or layout.size % 4:
                return False
            slot = (slot * 4 + layout.alignment - 1) // layout.alignment * layout.alignment // 4
            covered = 0
            for field in layout.fields:
                member = declarations.canonical(field.type, aliases)
                if field.fields or field.extent or field.bit_size is not None or field.offset != covered:
                    return False
                width = 8 if member in ("double", "long long", "unsigned long long") else 4
                if field.size != width or field.offset % width:
                    return False
                add(member, slot + field.offset // 4)
                covered += field.size
            if covered != layout.size:
                return False
            slot += layout.size // 4
            floating_prefix = False
        else:
            width = 2 if type_ in ("double", "long long", "unsigned long long") else 1
            if width == 2:
                slot += slot % 2
            fp = type_ in ("float", "double")
            add(type_, slot, ("f12" if i == 0 else "f14") if fp and floating_prefix and i < 2 else None)
            slot += width
            floating_prefix &= fp
    expected = machine["params"]
    if len(slots) != len(expected):
        return False
    for actual, wanted in zip(slots, expected, strict=True):
        if actual["register"] != wanted["register"]:
            return False
        # Pointer storage is one word; a void pointee supplies no layout claim.
        if wanted["type"] == "void *" and actual["type"].endswith(" *"):
            continue
        if actual["type"] != wanted["type"]:
            return False
    returned = declarations.canonical(signature["return"], aliases)
    reg = "f0" if returned in ("float", "double") else "r2" if returned != "void" else None
    return bool(machine["return"] == {"register": reg, "type": returned})


def admits(source: str, signature: dict[str, Any], machine: dict[str, Any], aliases: dict[str, str]) -> bool:
    """Incomplete or unsupported layout evidence remains a hold."""
    try:
        return equivalent(source, signature, machine, aliases)
    except Held:
        return False


def compatible_prototypes(left: str, right: str, aliases: dict[str, str]) -> bool:
    """Compare proven scalar/pointer entry transport, independent of pointee inference.

    Unknown or by-value aggregate transport needs a separate layout proof.
    Width, aligned argument slots, leading FP registers, return registers and
    variadic calling convention must all agree.
    """
    from pycparser import c_ast  # type: ignore[import-untyped]

    from unbake.layout.structs_types import SCALARS
    from unbake.typemap.header_names import type_identity

    def transport(prototype: str) -> object:
        tree = cdecl.parse(cdecl.declaration_source(prototype), typedefs=aliases)
        node = tree.ext[0]
        if len(tree.ext) != 1 or not isinstance(node, c_ast.Decl) or not isinstance(node.type, c_ast.FuncDecl):
            raise ValueError("not a single function prototype")
        if node.type.args is None:
            raise ValueError("unknown argument list")
        shape = type_identity(declarations.node_type(node.type), aliases)
        if not isinstance(shape, tuple) or shape[0] != "function":
            raise ValueError("not a function type")

        def value(type_: Any) -> tuple[int, bool]:
            if type_[0] == "qualified":
                return value(type_[2])
            if type_[0] == "pointer":
                return 4, False
            if type_[0] == "scalar":
                if type_[1] == "void":
                    return 0, False
                if type_[1] in SCALARS:
                    return SCALARS[type_[1]][0], type_[1] in ("float", "double", "f32", "f64")
            raise ValueError("unresolved value transport")

        variadic = bool(shape[2] and shape[2][-1] == ("variadic",))
        parameters = shape[2][:-1] if variadic else shape[2]
        slot = 0
        floating_prefix = not variadic
        slots = []
        for i, parameter in enumerate(parameters):
            width, floating = value(parameter)
            if not width:
                raise ValueError("void parameter")
            words = (width + 3) // 4
            if words == 2:
                slot += slot % 2
            register = (
                ("f12" if i == 0 else "f14")
                if floating and floating_prefix and i < 2
                else f"r{4 + slot}"
                if slot < 4
                else f"stack{slot * 4}"
            )
            slots.append((register, width))
            slot += words
            floating_prefix &= floating
        width, floating = value(shape[1])
        returned = ("f0" if floating else "r2" if width else None, width)
        return tuple(slots), returned, variadic

    try:
        return transport(left) == transport(right)
    except (Held, ValueError, cdecl.ParseError):
        return False


def compatible_definition(
    source: str, function: str, signature: dict[str, Any], expected: str, aliases: dict[str, str]
) -> bool:
    """Unused trailing word formals add no executable claim to an inferred ABI.

    Call only with complete measured transport and no authoritative C
    contract. Never remove a used formal, FP prefix, aligned pair, aggregate,
    or variadic parameter to make a definition fit.
    """
    from pycparser import c_ast

    from unbake.layout.structs_types import SCALARS

    if not signature.get("arity_known") or signature.get("variadic"):
        return False
    try:
        tree = cdecl.parse(cdecl.declaration_source(source), typedefs=aliases)
        definition = next(node for node in tree.ext if isinstance(node, c_ast.FuncDef) and node.decl.name == function)
    except (Held, cdecl.ParseError, StopIteration):
        return False
    used: set[str] = set()

    class Uses(c_ast.NodeVisitor):  # type: ignore[misc]
        def visit_ID(self, node: Any) -> None:
            used.add(node.name)

    Uses().visit(definition.body)
    params = list(signature["params"])
    while params:
        last = params[-1]
        type_ = declarations.canonical(last["type"], aliases)
        if last["name"] in used or not (
            type_.endswith(" *") or (type_ not in ("float", "double") and SCALARS.get(type_, (0,))[0] == 4)
        ):
            return False
        params.pop()
        candidate = (
            declarations.declarator(
                signature["return"],
                function
                + "("
                + (", ".join(declarations.declarator(p["type"], p["name"]) for p in params) or "void")
                + ")",
            )
            + ";"
        )
        if compatible_prototypes(candidate, expected, aliases):
            return True
    return False
