"""One configured compiler per item; alternatives are measured only when it is not exact."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from unbake.project import toolchain
from unbake.config import Project


def alternatives(project: Project, function: str) -> list[str]:
    """Configured compilers other than the item's own, in registry order."""
    configured = project.compiler_reference(function)
    return [ident for ident in toolchain.registry() if ident in project.compilers and ident != configured]


def selected(project: Project, function: str, ident: str) -> Project:
    """Apply one measured choice in memory, never to neighbouring items."""
    units = {name: value for name, value in project.units.items() if name != function}
    if ident != project.default_compiler:
        units[function] = ident
    return replace(project, units=units)


def build_choice(project: Project, function: str, exact: list[str]) -> tuple[str, str]:
    """Pick the deterministic build compiler among byte-equivalent exact candidates."""
    configured = project.compiler_reference(function)
    if configured in exact:
        return configured, "configured compiler is in the exact candidate set"
    return next(ident for ident in toolchain.registry() if ident in exact), "first exact member in registry order"


def fold_units(data: dict[str, Any], receipts: list[dict[str, Any]]) -> dict[str, Any]:
    """Record exact selections as exception units; the default needs no entry."""
    default = data["project"]["default_compiler"]
    units = dict(data.get("units", {}))
    for evidence in receipts:
        if not evidence.get("exact_candidates"):
            continue
        function, ident = evidence["function"], evidence["selected"]
        units.pop(function, None)
        if ident != default:
            units[function] = ident
    data = dict(data)
    if units:
        data["units"] = dict(sorted(units.items()))
    else:
        data.pop("units", None)
    return data
