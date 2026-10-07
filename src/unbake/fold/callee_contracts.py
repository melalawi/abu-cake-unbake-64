"""Preserve authored pointer contracts when generated pointee inference differs."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from unbake import cdecl
from unbake.config import Held, Project
from unbake.layout import index, redeclarations
from unbake.layout.header_context import Headers
from unbake.layout.split import Edit
from unbake.typemap import declarations, evidence, o32, types_db

_INCLUDE = re.compile(r'^\s*#\s*include\s*[<"]([^>"\n]+)[>"]', re.M)


def _signature(text: str, aliases: dict[str, str]) -> dict[str, Any] | None:
    try:
        tree = cdecl.parse(cdecl.declaration_source(text), typedefs=aliases)
        rows = list(
            declarations.declaration_rows(
                tree.ext,
                {},
                definitions=False,
                contracts=True,
                owned_source=None,
                resolve=declarations.resolver(aliases),
            )
        )
        return rows[0][2] if len(rows) == 1 and rows[0][0] == "functions" else None
    except (Held, cdecl.ParseError):
        return None


def _proved(old: str, new: str, record: dict[str, Any], aliases: dict[str, str]) -> bool:
    left, right = _signature(old, aliases), _signature(new, aliases)
    if left is None or right is None or not o32.compatible_prototypes(old, new, aliases):
        return False
    # This is only a pointee spelling change. Scalar/FP widths, return types,
    # cardinality, variadic convention and aggregate grouping remain strict.
    pairs = [(left["return"], right["return"])] + [
        (a["type"], b["type"]) for a, b in zip(left["params"], right["params"], strict=True)
    ]
    changed = False
    for a, b in pairs:
        a, b = declarations.canonical(a, aliases), declarations.canonical(b, aliases)
        if a != b:
            if not a.endswith(" *") or not b.endswith(" *"):
                return False
            changed = True
    if not changed:
        return False
    provenance = record.get("provenance", [])
    if isinstance(provenance, dict):
        provenance = [provenance]
    if any(row.get("kind") == "proven" for row in provenance):
        # An actual callee C definition owns its contract. Do not reinterpret
        # its semantic type merely because the physical pointer word agrees.
        return False
    abi = record.get("abi") or {}
    if not abi.get("arity_known") or abi.get("missing") or abi.get("conflicts"):
        return False
    types = {param["register"]: param.get("type") for param in record.get("params", [])}
    ordered = evidence.parameters(abi.get("registers", []), types)
    if ordered is None or right["registers"] != ordered:
        return False
    returned = declarations.canonical(right["return"], aliases)
    if returned == "void":
        return not abi.get("used_returns") and not abi.get("unproven_return_reads")
    if returned in ("long long", "unsigned long long") and not abi.get("return_pair_known"):
        return False
    return bool(
        abi.get("return_known")
        and abi.get("return_register") == ("f0" if returned in ("float", "double") else "r2")
        and (abi.get("return_width") == 8) == (returned in ("long long", "unsigned long long"))
    )


def plan(
    contents: dict[Path, str],
    generated: frozenset[Path],
    candidates: dict[str, str],
    records: dict[str, Any],
    aliases: dict[str, str],
    versions: tuple[str, ...],
) -> list[Edit]:
    """Stage generated declarations only; publication still proves every consumer."""
    edits = []
    for path in sorted(generated & contents.keys()):
        before = contents[path]
        after = before
        for start, end in reversed(redeclarations.spans(before)):
            old = before[start:end]
            names = cdecl.declarations(old).declared
            if len(names) != 1:
                continue
            name = next(iter(names))
            new = candidates.get(name)
            if new is not None and _proved(old, new, records.get(name, {}), aliases):
                after = after[:start] + new.strip() + after[end:]
        if after != before:
            edits.append(Edit(path, before, after, versions))
    return edits


def reconcile(
    project: Project, headers: Headers, text: str, function: str, versions: tuple[str, ...]
) -> tuple[Headers, list[Edit]]:
    """Select explicit authored imports, never an arbitrary alternate provider."""
    database = types_db.path(project)
    if not database.is_file():
        return headers, []
    names = set(index.load(project)["headers"])
    generated = frozenset(
        path
        for path in headers.texts
        if any(path.is_relative_to(root) and path.relative_to(root).as_posix() in names for root in project.include)
    )
    authored: dict[Path, str] = {}
    pending = list(_INCLUDE.findall(text))
    while pending:
        include = pending.pop()
        path = next((root / include for root in project.include if root / include in headers.texts), None)
        if path is None or path in generated or path in authored:
            continue
        authored[path] = headers.texts[path]
        pending.extend(_INCLUDE.findall(authored[path]))
    aliases = redeclarations.aliases([*authored.values(), text])
    candidates: dict[str, str] = {}
    for source in [*authored.values(), text]:
        for name, declaration in redeclarations.catalog(source).items():
            if name == function or _signature(declaration, aliases) is None:
                continue
            if name in candidates and not redeclarations.equivalent(candidates[name], declaration, aliases):
                raise Held("layout", f"layout.redeclaration.{name}: conflicting authored callee contracts")
            candidates[name] = declaration
    collisions = {
        name for path in generated for name in redeclarations.catalog(headers.texts[path]) if name in candidates
    }
    records = types_db.entries(database, "functions", collisions) if collisions else {}
    edits = plan(headers.texts, generated, candidates, records, aliases, versions)
    if not edits:
        return headers, []
    from dataclasses import replace

    from unbake.fold import imports

    context = Headers({**headers.texts, **{edit.path: edit.after for edit in edits}}, root=headers.root)
    # The authored pointee's typedef provider must travel with its generated
    # prototype, so existing consumers can include that header on its own.
    edits = [replace(edit, after=imports.resolve(project, context, edit.after, edits=tuple(edits))) for edit in edits]
    return Headers({**headers.texts, **{edit.path: edit.after for edit in edits}}, root=headers.root), edits
