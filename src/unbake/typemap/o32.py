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
