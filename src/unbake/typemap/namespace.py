"""Reconcile C ordinary-identifier categories with measured code identity."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from pycparser import c_ast  # type: ignore[import-untyped]

from unbake import cdecl
from unbake.config import Held


def code_names(inventory: Mapping[str, Any], symbols: dict[str, Any]) -> tuple[set[str], dict[str, set[int]]]:
    """Read interval metadata only; identifying code never decodes a machine body."""
    names = set(inventory)
    addresses: dict[str, set[int]] = {}
    for item in inventory.values():
        names.update(item.get("aliases", ()))
        for version, placement in item["versions"].items():
            if placement.get("name"):
                names.add(placement["name"])
            address = placement.get("address")
            if address is None:
                continue
            addresses.setdefault(version, set()).add(address)
            table = symbols.get(version, {})
            names.update(table.get(address, table.get(str(address), ())))
    return names, addresses


def reconcile(
    inventory: Mapping[str, Any],
    facts: dict[str, Any],
    functions: dict[str, Any],
    globals_: dict[str, Any],
    arrays: dict[str, Any],
    constraints: list[dict[str, Any]],
) -> set[str]:
    """An object receipt cannot turn measured code or a function contract into storage."""
    names, addresses = code_names(inventory, facts.get("symbols", {}))
    names.update(functions)
    for name in sorted(names & (facts["globals"].keys() | globals_.keys() | arrays.keys())):
        placements = facts["globals"].get(name, {}).get("versions", {})
        if any(row["address"] not in addresses.get(version, ()) for version, row in placements.items()):
            raise Held("solve", f"types.namespace: {name}: function identity conflicts with mapped object storage")
        record = globals_.pop(name, None)
        array = arrays.pop(name, None)
        if record is not None or array is not None:
            rejected = record if record is not None else array
            assert rejected is not None
            constraints.append(
                {
                    "kind": "symbol_category_conflict",
                    "entity": name,
                    "previous": "function",
                    "incoming": "object",
                    "declaration": rejected.get("declaration"),
                    "provenance": rejected["provenance"],
                    "resolution": "function identity retained; object declaration rejected",
                }
            )
    return names


def check(value: dict[str, Any], components: Mapping[Path, str]) -> None:
    """Refuse contradictory retained contracts before a renderer can overwrite a function."""
    from unbake.typemap.declaration_evidence import units

    names = set(value.get("function_symbols", ())) | value.get("functions", {}).keys()
    collisions = names & (value.get("globals", {}).keys() | value.get("arrays", {}).keys())
    if collisions:
        raise Held("headers", "headers.namespace: function/object records conflict: " + ", ".join(sorted(collisions)))
    if not names:
        return
    contracts = units(dict(components))
    typedefs = {name: unit for unit in contracts for name in unit.types}
    parsed: dict[str, Any] = {}

    def tree(text: str) -> Any:
        if text not in parsed:
            row = cdecl.declarations(text)
            parsed[text] = cdecl.parse(
                cdecl.declaration_source(text), typedefs=row.uses | value.get("typedefs", {}).keys()
            )
        return parsed[text]

    def function(type_: Any, seen: set[str]) -> bool:
        if isinstance(type_, c_ast.FuncDecl):
            return True
        if isinstance(type_, c_ast.TypeDecl) and isinstance(type_.type, c_ast.IdentifierType):
            identifiers = type_.type.names
            if len(identifiers) == 1 and identifiers[0] in typedefs and identifiers[0] not in seen:
                name = identifiers[0]
                for node in tree(typedefs[name].text).ext:
                    if isinstance(node, c_ast.Typedef) and node.name == name:
                        return function(node.type, seen | {name})
        return False

    for unit in contracts:
        if not unit.names & names:
            continue
        # Declarator structure distinguishes a function from a pointer-to-function
        # object, including typedef-based spellings. Names and prefixes do not.
        try:
            nodes = tree(unit.text).ext
        except Exception as error:
            raise Held("headers", f"headers.namespace: cannot classify {unit.path}: {error}") from error
        for node in nodes:
            if isinstance(node, c_ast.Typedef) and node.name in names:
                raise Held(
                    "headers", f"headers.namespace: {node.name}: typedef conflicts with function identity ({unit.path})"
                )
            if isinstance(node, c_ast.Decl) and node.name in names and not function(node.type, set()):
                raise Held(
                    "headers",
                    f"headers.namespace: {node.name}: retained object declaration conflicts with function identity "
                    f"({unit.path}: {unit.text.strip()})",
                )
