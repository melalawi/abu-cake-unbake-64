"""A published unit's facts as a source layer joined to a header layer.

A unit is its source preprocessed with every header it includes. Its facts used to be extracted from the
whole unit, so a change to any generated header invalidated every includer. Here each header is parsed
once per version on its own (its header part), and a source parses only its own text with the typedef
scope its headers give it (its source part). A unit's facts are assembled from the parts of the files its
preprocessor line markers name, in their order, as the whole-unit extraction sees them.

Texts are preprocessed with line markers to attribute each line to its file; the markers are blanked
before parsing, so every node reads as the whole-unit extraction reads it (no file coordinate).

A source that defines typedefs or aggregates, or a unit whose header text is split by another header,
keeps the whole-unit extraction: its own layouts depend on header layouts.
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from pycparser import c_ast  # type: ignore[import-untyped]

from unbake import cdecl
from unbake.config import Held, Project
from unbake.typemap import declarations

# Turns a line-marker or header path into the one spelling a cached value stores (see spelling).
Spell = Callable[[str], str]

_MARKER = re.compile(r'^#\s*(\d+)\s+"([^"]*)"[^\n]*$')
_KINDS = ("functions", "globals", "arrays")
_WORD = re.compile(r"\b[A-Za-z_]\w*\b")


def spelling(project: Project, machine_root: Path, text: str) -> str:
    """A line-marker or header path in the spelling cached values store, so a copied or moved project reads them.

    A path under the project root is its posix path from the root; one under the machine root is `@machine/` and
    its path from that root. A path with no root (cpp keeps the -I route it took) is taken from the project root.
    Any other path is refused."""
    clean = os.path.normpath(text)
    if not os.path.isabs(clean):
        return clean
    for root, prefix in ((project.root, ""), (machine_root, "@machine/")):
        try:
            return prefix + Path(clean).relative_to(root).as_posix()
        except ValueError:
            continue
    raise Held("solve", f"facts.paths: {text} is outside the project and the machine root")


def suffix(text: str) -> str:
    """The unit after its preprocessor source boundary."""
    _, marker, rest = text.partition(declarations.BOUNDARY + "\n")
    if not marker:
        raise Held("solve", "types.declaration: missing preprocessor source boundary")
    return rest


class Marked:
    """A line-marked preprocessed text: each line's file and line in it, the runs, and the text with markers blanked.

    A run is a stretch one file contributes, named by (file, its first line in that file). A header with
    includes contributes several runs, split where cpp enters a nested include."""

    def __init__(self, text: str, first: str, spell: Spell) -> None:
        """FIRST names the file of the lines before any marker (all of them in a text preprocessed without cpp)."""
        lines = text.split("\n")
        self.lines: list[tuple[str, int]] = []
        self.runs: list[tuple[str, int]] = [(first, 1)]
        current, number = first, 0
        for index, line in enumerate(lines):
            found = _MARKER.match(line) if line.startswith("#") else None
            if found is not None:
                lines[index] = ""
                name = spell(found[2])
                if not name.startswith("<"):
                    current, number = name, int(found[1]) - 1
                    if self.runs[-1][0] != current:
                        self.runs.append((current, number + 1))
                self.lines.append((current, number))
                continue
            number += 1
            self.lines.append((current, number))
        self.blank = "\n".join(lines)

    def at(self, node: Any) -> tuple[str, int]:
        """(file, line in that file) where a node starts."""
        line: int = node.coord.line
        return self.lines[line - 1]

    def only(self, name: str) -> str:
        """The blanked text with every line another file contributes emptied (line numbers kept)."""
        return "\n".join(
            line if owner == name else "" for line, (owner, _) in zip(self.blank.split("\n"), self.lines, strict=True)
        )


def _scope_names(node: Any) -> list[tuple[str, bool]]:
    """Identifiers a file-scope node adds to the parser's scope: typedef names True, others False."""
    if isinstance(node, c_ast.Typedef):
        return [(node.name, True)]
    found: list[tuple[str, bool]] = []
    declaration = node.decl if isinstance(node, c_ast.FuncDef) else node
    if isinstance(declaration, c_ast.Decl) and declaration.name:
        found.append((declaration.name, False))

    class Enumerators(c_ast.NodeVisitor):  # type: ignore[misc]
        def visit_Enumerator(self, value: Any) -> None:
            found.append((value.name, False))

    Enumerators().visit(node)
    return found


def _defined(node: Any) -> list[str]:
    """Aggregate layouts a file-scope node defines, by the name its layout record takes."""
    names: list[str] = []

    class Bodies(c_ast.NodeVisitor):  # type: ignore[misc]
        def visit_Struct(self, value: Any) -> None:
            if value.decls is not None and value.name:
                names.append(value.name)
            self.generic_visit(value)

        visit_Union = visit_Struct

    Bodies().visit(node)
    if isinstance(node, c_ast.Typedef):
        base = node.type.type if isinstance(node.type, c_ast.TypeDecl) else None
        if isinstance(base, (c_ast.Struct, c_ast.Union)) and not base.name and base.decls is not None:
            names.append(node.name)
    return names


def _direct(node: Any) -> bool:
    """A typedef naming an aggregate itself (not through another typedef or a declarator)."""
    return isinstance(node.type, c_ast.TypeDecl) and isinstance(node.type.type, (c_ast.Struct, c_ast.Union))


def _bare(row: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in row.items() if key != "provenance"}


def _portable_rows(found: list[tuple[str, str, dict[str, Any]]], aliases: dict[str, str]) -> list[str]:
    """Rewrite function rows as a whole-unit extraction does (its prefix shares no typedef); the new prototypes."""
    rewritten = []
    if aliases:
        for kind, name, row in found:
            if kind == "functions" and (prototype := declarations.portable(name, row, aliases)):
                rewritten.append(prototype)
    return rewritten


def header_part(text: str, header: Path, spell: Spell) -> dict[str, Any]:
    """One header's own facts, from a line-marked text of it preprocessed alone (its includes give it context).

    Every item carries its line in the header, so a unit places it in the run of the header it falls in."""
    own_file = spell(str(header))
    marked = Marked(suffix(text), own_file, spell)
    cleaned = declarations.cleaned_unit(marked.blank)
    try:
        tree = declarations.unit_tree(cleaned)
    except Exception as error:
        raise Held("solve", f"types.declaration: {header}: {cdecl.located(text, str(error))}") from error
    aliases: dict[str, str] = {}
    for node in tree.ext:
        if isinstance(node, c_ast.Typedef):
            aliases[node.name] = declarations.node_type(node.type)
    own = [(node, marked.at(node)[1]) for node in tree.ext if marked.at(node)[0] == own_file]
    resolve = declarations.resolver(aliases)
    found: dict[str, list[list[Any]]] = {}
    for label, contracts in (("contracts", True), ("definitions", False)):
        found[label] = [
            [line, kind, name, _bare(row)]
            for node, line in own
            for kind, name, row in declarations.declaration_rows(
                [node], {}, resolve, definitions=True, owned_source=None, contracts=contracts
            )
        ]
    _portable_rows([(kind, name, row) for _, kind, name, row in found["contracts"] + found["definitions"]], aliases)
    layouts, contributed, unknown, _ = _layout_items(cleaned, aliases, marked, own_file, own)
    return {
        "typedefs": [
            [line, node.name, declarations.node_type(node.type)]
            for node, line in own
            if isinstance(node, c_ast.Typedef)
        ],
        "scope": [[line, name, typedef] for node, line in own for name, typedef in _scope_names(node)],
        "contracts": found["contracts"],
        "definitions": found["definitions"],
        "layouts": layouts,
        "aliases": contributed,
        "refused": bool(unknown),
    }


def _layout_items(
    cleaned: str, aliases: dict[str, str], marked: Marked, file: str, own: list[tuple[Any, int]]
) -> tuple[list[list[Any]], list[list[Any]], list[str], dict[str, dict[str, Any]]]:
    """FILE's layout records [line, name, row] in record order, the typedef aliases it gives any record
    [line, record, alias, direct] (direct: a typedef of the aggregate itself, not through another typedef),
    the layout refusal if any, and every record of the text."""
    records, unknown, starts = declarations.layout_rows(cleaned, aliases)
    layouts = []
    for name, row in records.items():
        owner, line = marked.lines[starts[name]]
        if owner == file:
            layouts.append([line, name, _bare(row)])
    typedef_lines = {node.name: line for node, line in own if isinstance(node, c_ast.Typedef)}
    direct = {node.name for node, _ in own if isinstance(node, c_ast.Typedef) and _direct(node)}
    contributed = sorted(
        (
            [typedef_lines[alias], name, alias, alias in direct]
            for name, row in records.items()
            for alias in row["aliases"]
            if alias in typedef_lines
        ),
        key=lambda item: item[0],
    )
    return layouts, contributed, unknown, records


def _spans(runs: list[tuple[str, int]]) -> list[tuple[str, int, int | None]]:
    """(file, first line, first line of the file's next run) of each run."""
    result = []
    for index, (name, start) in enumerate(runs):
        stop = next((line for other, line in runs[index + 1 :] if other == name), None)
        result.append((name, start, stop))
    return result


def _within(items: list[list[Any]], start: int, stop: int | None) -> list[list[Any]]:
    """Items (line first, in line order) of one run."""
    return [item for item in items if item[0] >= start and (stop is None or item[0] < stop)]


def source_part(
    text: str, source: Path, headers: Mapping[str, dict[str, Any]], provenance: dict[str, Any], spell: Spell
) -> dict[str, Any] | None:
    """The source's own facts parsed in its headers' scope, or None when the unit needs whole-unit extraction.

    TEXT is the line-marked unit; HEADERS the header parts by marker path."""
    own = spell(str(source))
    marked = Marked(suffix(text), own, spell)
    missing = [name for name, _ in marked.runs if name != own and name not in headers]
    if missing:
        raise Held("solve", f"facts.headers: {source}: no header part for {missing[0]}")
    if any(headers[name]["refused"] for name, _ in marked.runs if name != own):
        return None
    scope: dict[str, bool] = {}
    aliases: dict[str, str] = {}
    for name, start, stop in _spans(marked.runs):
        if name != own:
            scope.update(
                (identifier, typedef) for _, identifier, typedef in _within(headers[name]["scope"], start, stop)
            )
            aliases.update((alias, type_) for _, alias, type_ in _within(headers[name]["typedefs"], start, stop))
    cleaned = declarations.cleaned_unit(marked.only(own))
    try:
        tree = cdecl.parser(scope).parse(cleaned)
    except Exception:
        # The whole unit parses it, or refuses with the error located in its own text.
        return None
    own_nodes = [(node, marked.at(node)[1]) for node in tree.ext]
    typedefs = [
        [line, node.name, declarations.node_type(node.type)]
        for node, line in own_nodes
        if isinstance(node, c_ast.Typedef)
    ]
    if any(name in aliases for _, name, _ in typedefs):
        # A typedef the source spells again changes how its headers' types resolve.
        return None
    aliases.update((name, type_) for _, name, type_ in typedefs)
    layouts: list[list[Any]] = []
    contributed: list[list[Any]] = []
    named: set[str] = set()
    if typedefs or any(_defined(node) for node, _ in own_nodes):
        # The source's own layouts need its headers' layouts: read them from the whole unit. The names they
        # spell let a unit whose header layouts changed since be extracted again (see dependencies).
        layouts, contributed, unknown, _ = _layout_items(
            declarations.cleaned_unit(marked.blank), aliases, marked, own, own_nodes
        )
        if unknown:
            return None
        named = {word for _, _, row in layouts for word in _WORD.findall(row["declaration"])}
        named |= {word for _, _, type_ in typedefs for word in _WORD.findall(type_)}
    resolve = declarations.resolver(aliases)
    found: dict[str, list[Any]] = {}
    for label, contracts in (("contracts", True), ("definitions", False)):
        found[label] = [
            [marked.at(node)[1], kind, name, _bare(row)]
            for node in tree.ext
            for kind, name, row in declarations.declaration_rows(
                [node], {}, resolve, definitions=True, owned_source=source, contracts=contracts
            )
        ]
    prototypes = _portable_rows(
        [(item[1], item[2], item[3]) for item in found["contracts"] + found["definitions"]], aliases
    )
    if prototypes:
        try:
            cdecl.parse("\n".join(prototypes), typedefs=scope)
        except Exception as error:
            raise Held("solve", f"types.declaration: {provenance}: emitted prototype: {error}") from error
    return {
        "runs": [[name, line] for name, line in marked.runs],
        **found,
        "typedefs": typedefs,
        "layouts": layouts,
        "aliases": contributed,
        "named": sorted(named),
    }


def _layout_identity(row: dict[str, Any]) -> dict[str, Any]:
    """What a dependent layout reads of another: everything but its alias list and provenance."""
    return {key: value for key, value in row.items() if key not in ("aliases", "provenance")}


def dependencies(context: Context, runs: list[list[Any]], own: str, named: list[str]) -> dict[str, Any]:
    """The header layouts a source's own layouts read: those NAMED spells, by name or alias, as its headers
    give them now. A source part is valid only against the same dependencies. OWN is the source's spelling."""
    if not named:
        return {}
    spans = tuple(span for span in _spans([(name, line) for name, line in runs]) if span[0] != own)
    words = set(named)
    return {
        name: _layout_identity(row)
        for name, row in _layouts(context.facts(spans)).items()
        if name in words or words & set(row["aliases"])
    }


_EMPTY: dict[str, Any] = {
    "contracts": {kind: {} for kind in _KINDS},
    "definitions": {kind: {} for kind in _KINDS},
    "aliases": {},
    "layouts": {},
    "contributed": [],
    "final": {},
}


class Context:
    """Facts of a unit's runs, built once per run list and extended from the list one run shorter."""

    def __init__(self, headers: Mapping[str, dict[str, Any]]) -> None:
        self.headers = headers
        self._built: dict[tuple[tuple[str, int, int | None], ...], dict[str, Any]] = {(): _EMPTY}

    def facts(
        self, spans: tuple[tuple[str, int, int | None], ...], own: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Facts of SPANS in order; a span of the source reads OWN (the source part), and is never remembered."""
        known = self._built.get(spans)
        if known is not None:
            return known
        base = self.facts(spans[:-1], own)
        name, start, stop = spans[-1]
        part = self.headers.get(name) if own is None or name in self.headers else own
        if part is None:
            raise Held(
                "solve", f"facts.headers: {name} is included by a source but has no header part for this version"
            )
        # Copy on write: a span that adds nothing of a kind shares its base's object, so units of one header
        # list share one alias map and one layout template (encoded and merged once).
        found: dict[str, Any] = {}
        for label in ("contracts", "definitions"):
            tables = dict(base[label])
            for _, kind, row_name, row in _within(part[label], start, stop):
                if tables[kind] is base[label][kind]:
                    tables[kind] = dict(tables[kind])
                tables[kind][row_name] = row
            found[label] = tables
        typedefs = _within(part.get("typedefs", []), start, stop)
        found["aliases"] = (
            {**base["aliases"], **{alias: type_ for _, alias, type_ in typedefs}} if typedefs else base["aliases"]
        )
        layouts = _within(part.get("layouts", []), start, stop)
        found["layouts"] = (
            {**base["layouts"], **{name: row for _, name, row in layouts}} if layouts else base["layouts"]
        )
        contributed = _within(part.get("aliases", []), start, stop)
        found["contributed"] = [*base["contributed"], *contributed] if contributed else base["contributed"]
        found["final"] = base["final"] if not layouts and not contributed else None
        if all(name in self.headers for name, _, _ in spans):
            self._built[spans] = found
        return found


def _layouts(built: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Layout records with the unit's alias lists: direct typedefs in order, then every alias in order."""
    if built["final"] is not None:
        return built["final"]  # type: ignore[no-any-return]
    contributed: dict[str, list[tuple[str, bool]]] = {}
    for _, name, alias, direct in built["contributed"]:
        contributed.setdefault(name, []).append((alias, direct))
    result = {}
    for name, row in built["layouts"].items():
        rows = contributed.get(name, [])
        aliases = list(dict.fromkeys([alias for alias, direct in rows if direct] + [alias for alias, _ in rows]))
        result[name] = {**row, "aliases": aliases} if aliases != row["aliases"] else row
    built["final"] = result
    return result


def assemble(
    context: Context,
    part: dict[str, Any],
    own: str,
    source_text: str,
    provenance: Callable[[str], dict[str, Any]],
    generated: frozenset[str],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """The unit's consumed contracts and its whole definition seed (every function it defines).

    OWN is the source's spelling. GENERATED names the headers the solver wrote (spelled): their declarations are
    the solver's own output, so they never come back as consumed evidence. The definition seed keeps them, because
    a source's own
    layouts embed included structs by value.

    PROVENANCE(kind) gives the provenance of the "published" contracts and of the "proven" definitions.
    """
    spans = tuple(_spans([(name, line) for name, line in part["runs"]]))
    first = next((index for index, (name, _, _) in enumerate(spans) if name == own), len(spans))
    context.facts(spans[:first])
    built = context.facts(spans, part)
    layouts = _layouts(built)
    published, proven = provenance("published"), provenance("proven")
    visible = context.facts(tuple(span for span in spans if span[0] not in generated), part)
    contract = {
        **visible["contracts"],
        "structs": _layouts(visible),
        "aliases": visible["aliases"],
        "shared_typedefs": {},
        "unknown": [],
    }
    consumed = declarations.consumed_contracts(contract, source_text)
    for kind in (*_KINDS, "structs"):
        consumed[kind] = {name: {**row, "provenance": published} for name, row in consumed[kind].items()}
    definition = {
        **{
            kind: {name: {**row, "provenance": proven} for name, row in rows.items()}
            for kind, rows in built["definitions"].items()
        },
        "structs": declarations.ProvenStructs(layouts, proven),
        "aliases": built["aliases"],
        "shared_typedefs": {},
        "unknown": [],
    }
    return consumed, definition
