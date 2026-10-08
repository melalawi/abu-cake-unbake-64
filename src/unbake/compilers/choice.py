"""One configured compiler per item; alternatives are measured only when it is not exact."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from unbake.compilers import registry as toolchain
from unbake.config import Project


def alternatives(project: Project, function: str) -> list[str]:
    """Configured compilers other than the item's own, in registry order."""
    configured = project.compiler_reference(function)
    return [ident for ident in toolchain.registry() if ident in project.compilers and ident != configured]


def selected(project: Project, function: str, ident: str) -> Project:
    """Apply one measured choice in memory, never to neighbouring items."""
    from unbake.compilers.recipe_options import UnitRecipe

    units = dict(project.units)
    key = project.unit_path(function)
    previous = project.recipe_for(function)
    units[key] = UnitRecipe(ident, previous.options, previous.functions)
    return replace(project, units=units)


def build_choice(project: Project, function: str, exact: list[str]) -> tuple[str, str]:
    """Pick the deterministic build compiler among byte-equivalent exact candidates."""
    configured = project.compiler_reference(function)
    if configured in exact:
        return configured, "configured compiler is in the exact candidate set"
    return next(ident for ident in toolchain.registry() if ident in exact), "first exact member in registry order"


def fold_units(data: dict[str, Any], receipts: list[dict[str, Any]]) -> dict[str, Any]:
    """Record exact selections as exception units; the default needs no entry."""
    from unbake.compilers.recipe_options import UnitRecipe

    units = dict(data.get("units", {}))
    for evidence in receipts:
        if not evidence.get("exact_candidates"):
            continue
        key = f"src/{evidence['function']}.c"
        previous = UnitRecipe.read(units[key]) if key in units else UnitRecipe(data["project"]["default_compiler"])
        units[key] = UnitRecipe(evidence["selected"], previous.options, previous.functions).document()
    return {**data, "units": dict(sorted(units.items()))}
