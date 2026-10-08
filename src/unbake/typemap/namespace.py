"""Reconcile C ordinary-identifier categories with measured code identity."""

from __future__ import annotations

import copy
import re
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from typing import Any

from pycparser import c_ast, c_generator  # type: ignore[import-untyped]

from unbake import cdecl
from unbake.config import Held, Host, Project
from unbake.process import capture
from unbake.process import named as cause_named


class FunctionDeclarations:
    """Repair address-only object receipts using code identity, never name prefixes.

    Existing function contracts win. Without a contract, retain the scalar
    spelling as an unspecified-parameter function declaration; it supplies
    linkage for an address reference without inventing a parameter list.
    """

    def __init__(self, value: dict[str, Any], contents: Mapping[Path, str]) -> None:
        from unbake.layout import redeclarations

        self.names = set(value.get("function_symbols", ())) | value.get("functions", {}).keys()
        self.records = value.get("functions", {})
        self.typedefs: dict[str, str] = {}
        self.parsed: dict[str, Any] = {}
        self.spans: dict[str, list[tuple[int, int]]] = {}
        self.rewritten: dict[str, str] = {}
        self.prototypes: dict[str, str] = {}
        self.aliases = set(value.get("typedefs", {}))
        if not self.names:
            return
        # Typedef-based function declarations need their actual declarator,
        # rather than the parser's temporary scalar typedef scaffolding.
        for text in dict.fromkeys(contents.values()):
            self.spans[text] = redeclarations.spans(text)
            for start, end in self.spans[text]:
                statement = text[start:end]
                if statement.startswith("typedef"):
                    for name in cdecl.declarations(statement).typedefs:
                        self.typedefs[name] = statement
        generator = c_generator.CGenerator()
        for text in dict.fromkeys(contents.values()):
            if self.names.isdisjoint(re.findall(r"\b[A-Za-z_]\w*\b", text)):
                continue
            for start, end in self.spans[text]:
                statement = text[start:end]
                if self.names.isdisjoint(re.findall(r"\b[A-Za-z_]\w*\b", statement)):
                    continue
                for node in self.tree(statement).ext:
                    if isinstance(node, c_ast.Decl) and node.name in self.names and self.function(node.type):
                        self.prototypes.setdefault(node.name, generator.visit(node) + ";")

    def tree(self, text: str) -> Any:
        if text not in self.parsed:
            row = cdecl.declarations(text)
            self.parsed[text] = cdecl.parse(cdecl.declaration_source(text), typedefs=row.uses | self.aliases)
        return self.parsed[text]

    def function(self, type_: Any, seen: frozenset[str] = frozenset()) -> bool:
        if isinstance(type_, c_ast.FuncDecl):
            return True
        if isinstance(type_, c_ast.TypeDecl) and isinstance(type_.type, c_ast.IdentifierType):
            identifiers = type_.type.names
            if len(identifiers) == 1 and identifiers[0] in self.typedefs and identifiers[0] not in seen:
                name = identifiers[0]
                for node in self.tree(self.typedefs[name]).ext:
                    if isinstance(node, c_ast.Typedef) and node.name == name:
                        return self.function(node.type, seen | {name})
        return False

    def rewrite(self, text: str) -> str:
        from unbake.layout import redeclarations

        if text in self.rewritten:
            return self.rewritten[text]
        after = text
        if not self.names.isdisjoint(re.findall(r"\b[A-Za-z_]\w*\b", text)):
            generator = c_generator.CGenerator()
            if text not in self.spans:
                self.spans[text] = redeclarations.spans(text)
            for start, end in reversed(self.spans[text]):
                statement = text[start:end]
                if self.names.isdisjoint(re.findall(r"\b[A-Za-z_]\w*\b", statement)):
                    continue
                nodes = self.tree(statement).ext
                rows = []
                changed = False
                for node in nodes:
                    if isinstance(node, c_ast.Decl) and node.name in self.names and not self.function(node.type):
                        if node.init is not None or "extern" not in node.storage:
                            raise Held(
                                cause_named(
                                    "headers.namespace",
                                    f"headers.namespace: {node.name}: function identity has object storage",
                                    owner="typemap.namespace",
                                    stage="headers",
                                )
                            )
                        record = self.records.get(node.name, {})
                        prototype = self.prototypes.get(node.name) or record.get("prototype")
                        if not prototype:
                            if not isinstance(node.type, c_ast.TypeDecl):
                                raise Held(
                                    cause_named(
                                        "headers.namespace",
                                        (
                                            f"headers.namespace: {node.name}: non-scalar object conflicts with "
                                            f"function identity"
                                        ),
                                        owner="typemap.namespace",
                                        stage="headers",
                                    )
                                )
                            declaration = copy.deepcopy(node)
                            declaration.type = c_ast.FuncDecl(None, declaration.type)
                            prototype = generator.visit(declaration) + ";"
                        rows.append(prototype)
                        changed = True
                    else:
                        rows.append(generator.visit(node) + ";")
                if changed:
                    after = after[:start] + "\n".join(rows) + after[end:]
        self.rewritten[text] = after
        return after


def project_declarations(
    project: Project, contents: Mapping[Path, str], *, texts: tuple[str, ...] = ()
) -> FunctionDeclarations:
    """Read code placement once per VERSION and only requested solved contracts."""
    from unbake.layout import split
    from unbake.typemap import types_db

    names: set[str] = set()
    for version in project.versions:
        for row in split.functions(project, version):
            names.update(row.aliases)
            names.update(name for name, _offset in row.entries)
        for name, (_address, _line, match) in split.symbols(project.version(version).symbols)[1].items():
            if re.search(r"\btype\s*:\s*func\b", match.string):
                names.add(name)
    words = set(re.findall(r"\b[A-Za-z_]\w*\b", "\n".join((*contents.values(), *texts))))
    names &= words
    database = types_db.path(project)
    records = types_db.entries(database, "functions", names) if names and database.is_file() else {}
    return FunctionDeclarations({"function_symbols": names, "functions": records}, contents)


def publication_outputs(
    contracts: FunctionDeclarations,
    headers: Mapping[Path, str],
    sources: Mapping[Path, str],
    outputs: Mapping[Path, bytes | Path],
) -> dict[Path, bytes]:
    """Stage repaired installed consumers and homes with the normal publication.

    Prefer an already rendered output over its installed predecessor. Even a
    home retained until the headers step must stop declaring code as storage.
    """
    changes = {}
    for path, before in {**headers, **sources}.items():
        if contracts.rewrite(before) == before:
            continue
        content = outputs.get(path)
        current = (
            before if content is None else (content.read_bytes() if isinstance(content, Path) else content).decode()
        )
        changes[path] = contracts.rewrite(current).encode()
    return changes


def consumer_edits(
    project: Project, contracts: FunctionDeclarations, function: str, edits: tuple[Any, ...]
) -> tuple[Any, ...]:
    """Canonicalize installed consumers once, preserving any pending layout edit."""
    from unbake.layout import split
    from unbake.layout.split import Edit

    planned = {edit.path: edit for edit in edits}
    owners = None
    for source in sorted(project.src.glob("*.c")):
        if source.stem == function:
            continue
        existing = planned.get(source)
        before = existing.before if existing is not None else source.read_text()
        text = existing.after if existing is not None else before
        after = contracts.rewrite(text)
        if after == text:
            continue
        if owners is None:
            owners = {version: split.owners_by_alias(project, version) for version in project.versions}
        versions = tuple(
            version
            for version in split.code_versions(project, source.stem, owners)
            if any(row.kind == "c" for row in owners[version].get(source.stem, ()))
        )
        if versions:
            planned[source] = Edit(source, before, after, versions)
    return tuple(planned.values())


@contextmanager
def comparison_view(
    project: Project,
    host: Host,
    source: Path,
    text: str,
    *,
    contents: Mapping[Path, str] | None = None,
    contracts: FunctionDeclarations | None = None,
) -> Iterator[tuple[Project, Path]]:
    """Compare the same canonical declarations that folding will publish."""
    from unbake import atomic, scratch
    from unbake.layout.header_context import Headers
    from unbake.project.headers import include_headers

    if contents is None:
        contents = Headers.contents(project)
    if contracts is None:
        contracts = project_declarations(project, contents, texts=(text,))
    after = contracts.rewrite(text)
    changed = {
        path: rewritten for path, before in contents.items() if (rewritten := contracts.rewrite(before)) != before
    }
    if after == text and not changed:
        yield project, source
        return
    with scratch.temporary(host, project, "fold", prefix="function-declarations-") as temporary:
        root = Path(temporary)
        for path, rewritten in changed.items():
            relative = next(path.relative_to(home) for home in project.include if path.is_relative_to(home))
            atomic.text(root / "include" / relative, rewritten)
        for path, name in include_headers(project):
            mirror = root / "include" / name
            if not mirror.exists():
                mirror.parent.mkdir(parents=True, exist_ok=True)
                mirror.symlink_to(path)
        candidate = source
        if after != text:
            candidate = root / "src" / source.name
            atomic.text(candidate, after)
        yield replace(project, work_include=(root / "include", *project.work_include)), candidate


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
            raise Held(
                cause_named(
                    "types.namespace",
                    f"types.namespace: {name}: function identity conflicts with mapped object storage",
                    owner="typemap.namespace",
                    stage="solve",
                )
            )
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
        raise Held(
            cause_named(
                "typemap.namespace.check",
                "headers.namespace: function/object records conflict: " + ", ".join(sorted(collisions)),
                owner="typemap.namespace",
                stage="headers",
            )
        )
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
            raise Held(
                capture(
                    error,
                    cause=cause_named(
                        "headers.namespace",
                        f"headers.namespace: cannot classify {unit.path}: {error}",
                        owner="typemap.namespace",
                        stage="headers",
                    ),
                )
            ) from error
        for node in nodes:
            if isinstance(node, c_ast.Typedef) and node.name in names:
                raise Held(
                    cause_named(
                        "headers.namespace",
                        f"headers.namespace: {node.name}: typedef conflicts with function identity ({unit.path})",
                        owner="typemap.namespace",
                        stage="headers",
                    )
                )
            if isinstance(node, c_ast.Decl) and node.name in names and not function(node.type, set()):
                raise Held(
                    cause_named(
                        "headers.namespace",
                        (
                            f"headers.namespace: {node.name}: retained object declaration conflicts "
                            f"with function identity ({unit.path}: {unit.text.strip()})"
                        ),
                        owner="typemap.namespace",
                        stage="headers",
                    )
                )
