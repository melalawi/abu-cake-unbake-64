"""Resolve complete authored aggregates by layout before shared field folding."""

from __future__ import annotations

import json
import re
from collections import ChainMap
from collections.abc import Mapping, Sequence

from unbake.layout.structs import Field, Layout, held


def identity(record: Layout, index: Mapping[str, Layout]) -> str:
    """Include pointee evidence and recursive edges, excluding aggregate tag spelling."""

    def shape(item: Layout, active: tuple[str, ...]) -> object:
        if item.name in active:
            return ("recursive", active.index(item.name))
        active = (*active, item.name)

        def member(field: Field) -> object:
            def aggregate(match: re.Match[str]) -> str:
                name = match[2]
                target = index.get(name)
                value = shape(target, active) if target else ("opaque", match[1], name)
                return json.dumps(value, separators=(",", ":"))

            spelling = re.sub(r"\b(struct|union)\s+(\w+)", aggregate, field.type)
            # The parser already resolves scalar aliases and declarator extents.
            spelling = {
                "s8": "signed char",
                "u8": "unsigned char",
                "s16": "short",
                "u16": "unsigned short",
                "s32": "int",
                "u32": "unsigned int",
                "s64": "long long",
                "u64": "unsigned long long",
                "f32": "float",
                "f64": "double",
            }.get(spelling, spelling)
            return (
                field.offset,
                field.size,
                tuple(int(extent) for extent in re.findall(r"\[(\d+)\]", field.type)),
                spelling,
                field.bit_offset,
                field.bit_size,
                tuple(member(child) for child in field.fields),
            )

        return item.kind, item.size, item.alignment, tuple(member(field) for field in item.fields)

    return json.dumps(shape(record, ()), separators=(",", ":"))


class Index:
    """Shared layouts by name and by layout identity, extended as declarations are added."""

    def __init__(self, existing: Sequence[Layout]) -> None:
        self.names: dict[str, Layout] = {}
        self.available: dict[str, list[Layout]] = {}
        self.add(existing)

    def add(self, records: Sequence[Layout]) -> None:
        for item in records:
            for name in (item.name, *item.aliases):
                self.names.setdefault(name, item)
        for item in records:
            self.available.setdefault(identity(item, self.names), []).append(item)

    def resolve(
        self, records: list[Layout], owner: str, *, reserved: set[str] | None = None
    ) -> dict[str, tuple[str, Layout]]:
        """Reuse an equal shared layout; a conflicting name takes its owning function and size."""
        names = ChainMap({name: item for item in records for name in (item.name, *item.aliases)}, self.names)
        preferred: dict[str, set[str]] = {}

        def pointees(local: tuple[Field, ...], shared: tuple[Field, ...]) -> None:
            for field, target in zip(local, shared, strict=True):
                left = re.findall(r"\b(?:struct|union)\s+(\w+)", field.type)
                right = re.findall(r"\b(?:struct|union)\s+(\w+)", target.type)
                for before, after in zip(left, right, strict=True):
                    if before in names and after in self.names:
                        preferred.setdefault(before, set()).add(after)
                pointees(field.fields, target.fields)

        # A reused parent fixes the C tag of each of its pointees. Choose that
        # provider for an equal local child instead of an unrelated equal layout.
        for item in records:
            candidates = self.available.get(identity(item, names), [])
            if candidates:
                parent = min(candidates, key=lambda candidate: (candidate.name != item.name, candidate.name))
                pointees(item.fields, parent.fields)
        occupied = set(self.names) | (reserved or set())
        result: dict[str, tuple[str, Layout]] = {}
        requested: dict[str, tuple[str, Layout]] = {}
        for item in records:
            key = identity(item, names)
            candidates = [
                candidate for candidate in self.available.get(key, []) if candidate.name not in (reserved or set())
            ]
            if candidates:
                choices = preferred.get(item.name, set())
                evidence = min(
                    candidates,
                    key=lambda candidate: (candidate.name not in choices, candidate.name != item.name, candidate.name),
                )
                target = evidence.name
            elif key in requested:
                target, evidence = requested[key]
            elif item.name not in occupied:
                target = item.name
                evidence = item
            else:
                target = f"{item.name}_{owner}"
                if target in occupied:
                    target = f"{item.name}_{owner}_{item.size:#x}"
                evidence = item
                if target in occupied:
                    held(target, "owner layout name conflicts with a different shared declaration")
            occupied.add(target)
            requested[key] = target, evidence
            for name in (item.name, *item.aliases):
                if name in result and result[name][0] != target:
                    held(name, "different layouts in one source view")
                result[name] = target, evidence
        return result
