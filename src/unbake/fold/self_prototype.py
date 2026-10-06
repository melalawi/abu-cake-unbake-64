"""Retire obsolete volatile qualifiers from a republished function's own prototype."""

from __future__ import annotations

from pathlib import Path

from pycparser import c_ast, c_generator  # type: ignore[import-untyped]

from unbake import cdecl
from unbake.layout import redeclarations
from unbake.layout.split import Edit
from unbake.typemap.declarations import _declaration_unit


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
