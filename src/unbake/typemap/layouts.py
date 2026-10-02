"""Observed storage prefixes, independent of complete object or function types."""

from __future__ import annotations

from typing import Any

from unbake.layout.structs_types import SCALARS
from unbake.typemap import declarations, evidence


def observed(
    name: str,
    origin: str,
    offsets: dict[int, list[dict[str, Any]]],
    resolved: dict[str, dict[str, Any]],
    users: list[str],
) -> dict[str, Any]:
    record: dict[str, Any] = {
        "state": "unknown",
        "type": None,
        "partial": True,
        "common_base": origin,
        "users": users,
        "size": None,
        "declaration": None,
        "observed_offsets": sorted(offsets),
        "provenance": [access for rows in offsets.values() for access in rows],
    }
    fields = []
    cursor = 0
    lines = [f"struct {name} {{"]
    for offset, accesses in sorted(offsets.items()):
        widths = sorted({a["width"] for a in accesses})
        if offset < cursor or offset < 0 or len(widths) != 1 or widths[0] not in (1, 2, 4, 8):
            return {**record, "reason": "overlapping, negative or inconsistent observed storage intervals"}
        width = widths[0]
        if any(a["partial"] for a in accesses):
            return {**record, "reason": "partial memory transfers do not establish field storage boundaries"}
        state = resolved.get(f"field:{origin}:{offset}", {})
        type_ = state.get("type")
        size, alignment = (4, 4) if type_ and type_.endswith(" *") else SCALARS.get(type_ or "", (0, 0))
        representations = {value for a in accesses if (value := evidence.scalar(a)) is not None}
        # Word loads can represent pointers. Other representations come from
        # these accesses, rather than unrelated seeds in a flow component.
        pointer = type_ and type_.endswith(" *") and representations <= {"int", "unsigned int"}
        if state.get("state") != "known" or size != width or not pointer:
            type_ = next(iter(representations)) if len(representations) == 1 else None
            size, alignment = SCALARS.get(type_ or "", (0, 0))
        known = type_ is not None and size == width and alignment is not None and offset % alignment == 0
        field_name = f"field_{offset:X}" if known else f"unknown_{offset:X}"
        if offset > cursor:
            lines.append(f"    unsigned char padding_{cursor:X}[{offset - cursor}];")
        if known:
            assert type_ is not None
            lines.append("    " + declarations.declarator(type_, field_name) + ";")
        else:
            lines.append(f"    unsigned char {field_name}[{width}];")
        fields.append(
            {
                "offset": offset,
                "size": width,
                "widths": widths,
                "name": field_name,
                "state": "known" if known else "unknown",
                "type": type_ if known else None,
                "reason": None if known else "field representation is unknown, conflicting or unaligned",
            }
        )
        cursor = offset + width
    lines.append("};")
    return {
        **record,
        "state": "known",
        "type": f"struct {name}",
        "fields": fields,
        "minimum_size": cursor,
        "declaration": "\n".join(lines),
    }
