"""Retire obsolete volatile qualifiers from a republished function's own prototype."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any

from pycparser import c_ast, c_generator  # type: ignore[import-untyped]

from unbake import cdecl
from unbake.layout import redeclarations
from unbake.layout.split import Edit
from unbake.typemap import declarations, evidence, o32
from unbake.typemap.declarations import _declaration_unit
from unbake.work.attempts import Attempt


def exact(
    contents: dict[Path, str],
    generated: frozenset[Path],
    text: str,
    authored: str,
    function: str,
    record: dict[str, Any],
    aliases: dict[str, str],
    versions: tuple[str, ...],
    attempt: Attempt | None,
) -> list[Edit]:
    """A matched own definition replaces an unowned inferred entry, provisionally.

    Unlike inference, this authority is the actual complete C implementation,
    matched in every holding version. The publication must reprove its folded
    body AND all published consumers of the changed headers before writing.
    Authored declaration evidence and unsupported aggregate/variadic transport
    remain outside this reconciliation.
    """
    from unbake.fold.callee_contracts import _signature
    from unbake.layout.structs_types import SCALARS

    if (
        attempt is None
        or attempt.function != function
        or attempt.sha256 != hashlib.sha256(authored.encode()).hexdigest()
        or not versions
        or set(attempt.versions) != set(versions)
        or any(row.get("exact") is not True or row.get("fault") for row in attempt.versions.values())
    ):
        return []
    provenance = record.get("provenance", [])
    if isinstance(provenance, dict):
        provenance = [provenance]
    if any(
        row.get("kind") in ("proven", "published", "declared") and row.get("function") in (None, function)
        for row in provenance
    ):
        return []
    abi = record.get("abi") or {}
    if abi.get("missing") or abi.get("conflicts") or abi.get("unproven_return_reads"):
        return []
    unit = _declaration_unit(cdecl.declaration_source(text))
    names = cdecl.declarations(unit)
    try:
        tree = cdecl.parse(unit, typedefs=names.uses | names.typedefs | aliases.keys())
    except Exception:
        return []
    definitions = [node.decl for node in tree.ext if isinstance(node, c_ast.FuncDef) and node.decl.name == function]
    if len(definitions) != 1 or "static" in definitions[0].storage:
        return []
    prototype = c_generator.CGenerator().visit(definitions[0]) + ";"
    right = _signature(prototype, aliases)

    def scalar(signature: dict[str, Any] | None) -> bool:
        if signature is None or signature["variadic"]:
            return False
        types = [signature["return"], *(p["type"] for p in signature["params"])]
        return all(
            (value := declarations.canonical(type_, aliases)) in SCALARS or value == "void" or value.endswith(" *")
            for type_ in types
        )

    if not scalar(right) or right is None or not right["arity_known"]:
        return []
    words = o32.argument_words(right, aliases)
    if abi.get("inputs") and (
        set(abi["inputs"]) != set(versions) or any(not set(row) <= words for row in abi["inputs"].values())
    ):
        return []
    returned = declarations.canonical(right["return"], aliases)
    result_register = "f0" if returned in ("float", "double") else "r2" if returned != "void" else None
    if abi and (
        (returned != "void" and not abi.get("return_known"))
        or any(reg != result_register for reg in abi.get("used_returns", []))
        or (abi.get("return_width") == 8 and returned not in ("double", "long long", "unsigned long long"))
    ):
        return []
    # A generated home can contain independently authored declarations. Their
    # evidence marker belongs to the declaration, not to the entire header.
    edits = []
    for path, before in sorted(contents.items()):
        after = before
        for start, end in reversed(redeclarations.spans(before)):
            old = before[start:end]
            if cdecl.declarations(old).declared != {function}:
                continue
            if redeclarations.equivalent(old, prototype, aliases):
                continue
            marked = re.search(
                r"/\* unbake (?:published declaration|declaration evidence):[^*]*\*/\s*$", before[:start]
            )
            if path not in generated or marked:
                return []
            left = _signature(old, aliases)
            if not scalar(left) or left is None:
                return []
            if left["arity_known"] and not words <= o32.argument_words(left, aliases):
                return []
            # This unit's scalar-word spelling changes and unused word formals
            # do not establish a new FP prefix or 64-bit/aggregate grouping.
            strict = {"float", "double", "long long", "unsigned long long"}
            for index, param in enumerate(left["params"]):
                previous = declarations.canonical(param["type"], aliases)
                current = (
                    declarations.canonical(right["params"][index]["type"], aliases)
                    if index < len(right["params"])
                    else None
                )
                if (previous in strict or current in strict) and previous != current:
                    return []
            after = after[:start] + prototype + after[end:]
        if after != before:
            edits.append(Edit(path, before, after, versions))
    return edits


def inferred(
    contents: dict[Path, str],
    generated: frozenset[Path],
    text: str,
    function: str,
    record: dict[str, Any],
    aliases: dict[str, str],
    versions: tuple[str, ...],
) -> list[Edit]:
    """Stage the definition's own entry spelling against an inferred prototype.

    This is not a callee or caller contract override. Publication still compiles
    the complete source and proves every holding version against its ROM.
    """
    from unbake.fold.callee_contracts import _signature

    provenance = record.get("provenance", [])
    if isinstance(provenance, dict):
        provenance = [provenance]
    if any(row.get("kind") in ("proven", "published") for row in provenance):
        return []
    abi = record.get("abi") or {}
    if not abi.get("arity_known") or abi.get("missing") or abi.get("conflicts"):
        return []
    unit = _declaration_unit(cdecl.declaration_source(text))
    names = cdecl.declarations(unit)
    try:
        tree = cdecl.parse(unit, typedefs=names.uses | names.typedefs | aliases.keys())
    except Exception:
        return []
    definitions = [node.decl for node in tree.ext if isinstance(node, c_ast.FuncDef) and node.decl.name == function]
    if len(definitions) != 1 or "static" in definitions[0].storage:
        return []
    prototype = c_generator.CGenerator().visit(definitions[0]) + ";"
    right = _signature(prototype, aliases)
    if right is None or not right["arity_known"] or right["variadic"]:
        return []
    types = {param["register"]: param.get("type") for param in record.get("params", [])}
    if right["registers"] != evidence.parameters(abi.get("registers", []), types):
        return []
    edits = []
    for path in sorted(generated & contents.keys()):
        before = contents[path]
        after = before
        for start, end in reversed(redeclarations.spans(before)):
            old = before[start:end]
            if cdecl.declarations(old).declared != {function}:
                continue
            if re.search(r"/\* unbake (?:published declaration|declaration evidence):[^*]*\*/\s*$", before[:start]):
                continue
            left = _signature(old, aliases)
            if left is None or not left["arity_known"] or left["variadic"]:
                continue
            if len(left["params"]) != len(right["params"]):
                continue
            pairs = [(a["type"], b["type"]) for a, b in zip(left["params"], right["params"], strict=True)]
            if any(
                declarations.canonical(a, aliases) != declarations.canonical(b, aliases)
                and not (
                    declarations.canonical(a, aliases).endswith(" *")
                    and declarations.canonical(b, aliases).endswith(" *")
                )
                for a, b in pairs
            ):
                continue
            returned = declarations.canonical(right["return"], aliases)
            previous_return = declarations.canonical(left["return"], aliases)
            transport = prototype
            if returned == "void" and previous_return != "void":
                if (
                    previous_return not in ("int", "unsigned int", "long", "unsigned long")
                    or abi.get("used_returns")
                    or abi.get("unproven_return_reads")
                ):
                    continue
                transport = (
                    declarations.declarator(
                        previous_return,
                        function
                        + "("
                        + ", ".join(declarations.declarator(p["type"], p["name"]) for p in right["params"])
                        + ")",
                    )
                    + ";"
                )
                if not right["params"]:
                    transport = declarations.declarator(previous_return, function + "(void)") + ";"
            elif previous_return != returned:
                continue
            if not o32.compatible_prototypes(old, transport, aliases):
                continue
            if not redeclarations.equivalent(old, prototype, aliases):
                after = after[:start] + prototype + after[end:]
        if after != before:
            edits.append(Edit(path, before, after, versions))
    return edits


def unqualify(contents: dict[Path, str], text: str, function: str, versions: tuple[str, ...] = ()) -> list[Edit]:
    """Only update an otherwise equivalent own signature; the land still proves its bytes."""
    candidates = {path: body for path, body in contents.items() if function in body and "volatile" in body}
    if not candidates:
        return []
    unit = _declaration_unit(cdecl.declaration_source(text))
    names = cdecl.declarations(unit)
    try:
        tree = cdecl.parse(unit, typedefs=names.uses | names.typedefs)
    except Exception:
        return []
    definitions = [node.decl for node in tree.ext if isinstance(node, c_ast.FuncDef) and node.decl.name == function]
    if len(definitions) != 1:
        return []
    prototype = c_generator.CGenerator().visit(definitions[0]) + ";"
    if any(token[0] == "volatile" for token in cdecl.SOURCE_TOKEN.finditer(prototype)):
        return []
    edits = []
    for path, body in candidates.items():
        replacements = []
        for start, end in redeclarations.spans(body):
            declaration = body[start:end]
            if cdecl.declarations(declaration).declared != {function}:
                continue
            plain = cdecl.SOURCE_TOKEN.sub(lambda token: "" if token[0] == "volatile" else token[0], declaration)
            if plain != declaration and redeclarations.equivalent(plain, prototype, {}):
                replacements.append((start, end, plain))
        updated = body
        for start, end, plain in reversed(replacements):
            updated = updated[:start] + plain + updated[end:]
        if updated != body:
            edits.append(Edit(path, body, updated, versions))
    return edits
