"""Reconcile C ordinary-identifier categories with measured code identity."""

from __future__ import annotations

import copy
import re
from collections.abc import Iterable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from typing import Any

from pycparser import c_ast, c_generator  # type: ignore[import-untyped]

from unbake import cdecl
from unbake.config import Held, Host, Project
from unbake.process import capture
from unbake.process import named as cause_named


def _scan(text: str) -> tuple[list[tuple[int, int]], dict[str, str]]:
    from unbake.layout import redeclarations

    spans = redeclarations.spans(text)
    typedefs: dict[str, str] = {}
    for start, end in spans:
        statement = text[start:end]
        if statement.startswith("typedef"):
            for name in cdecl.declarations(statement).typedefs:
                typedefs[name] = statement
    return spans, typedefs


def _prototypes(row: FunctionDeclarations, text: str) -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    if row.names.isdisjoint(re.findall(r"\b[A-Za-z_]\w*\b", text)):
        return found
    generator = c_generator.CGenerator()
    spans = row.spans.get(text)
    if spans is None:
        spans = row.spans[text] = _scan(text)[0]
    for start, end in spans:
        statement = text[start:end]
        if row.names.isdisjoint(re.findall(r"\b[A-Za-z_]\w*\b", statement)):
            continue
        for node in row.tree(statement).ext:
            if isinstance(node, c_ast.Decl) and node.name in row.names and row.function(node.type):
                found.append((node.name, generator.visit(node) + ";"))
    return found


def _fan_out(host: Host, cache_root: Path | None, job: Any, shared: Any, texts: list[str]) -> list[Any]:
    """JOB over every text in the pool; the shared value and cache root go to each worker once."""
    from unbake import pool, tui

    batches = [texts[start : start + 64] for start in range(0, len(texts), 64)]
    with tui.task("Reading header declarations", len(texts)):
        done = pool.run(host, job, batches, (cache_root, shared))
    return [row for batch in done for row in batch]


def _cached(cache_root: Path, kind: str, parts: tuple[str, ...], compute: Any) -> Any:
    from unbake import cache

    return cache.Cache(cache_root).value(kind, cache.key(*parts), cache.JSON, compute)


def _scan_job(shared: Any, texts: list[str]) -> list[Any]:
    root, _ = shared
    rows = []
    for text in texts:
        spans, typedefs = _cached(root, "namespace-scan", (text,), lambda text=text: list(_scan(text)))
        rows.append(([(a, b) for a, b in spans], typedefs))
    return rows


def _prototype_job(shared: Any, texts: list[str]) -> list[Any]:
    root, (names, aliases, typedefs) = shared
    from unbake import cache

    row = FunctionDeclarations._worker(names, aliases, typedefs)
    contracts = cache.key(repr(names), repr(aliases), repr(sorted(typedefs.items())))
    return [
        _cached(root, "namespace-prototypes", (contracts, text), lambda text=text: _prototypes(row, text))
        for text in texts
    ]


def _rewrite_job(shared: Any, texts: list[str]) -> list[str | None]:
    root, (names, aliases, typedefs, prototypes, records) = shared
    from unbake import cache

    row = FunctionDeclarations._worker(names, aliases, typedefs)
    row.prototypes, row.records = prototypes, records
    contracts = cache.key(repr(names), repr(aliases), repr(sorted(typedefs.items())), repr(sorted(prototypes.items())))
    contracts = cache.key(contracts, repr(sorted((k, repr(v)) for k, v in records.items())))
    rows: list[str | None] = []
    for text in texts:
        try:
            rows.append(_cached(root, "namespace-rewrite", (contracts, text), lambda text=text: row.rewrite(text)))
        except Held:
            rows.append(None)
    return rows


class FunctionDeclarations:
    """Repair address-only object receipts using code identity, never name prefixes.

    Existing function contracts win. Without a contract, retain the scalar
    spelling as an unspecified-parameter function declaration; it supplies
    linkage for an address reference without inventing a parameter list.
    """

    def __init__(
        self,
        value: dict[str, Any],
        contents: Mapping[Path, str],
        *,
        host: Host | None = None,
        cache_root: Path | None = None,
    ) -> None:
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
        texts = list(dict.fromkeys(contents.values()))
        # Parsing every header is a pure function of its text: pooled across the workers and kept in the project
        # cache, so a later landing reads what an earlier one parsed.
        scanned = (
            _fan_out(host, cache_root, _scan_job, (), texts)
            if host is not None and cache_root is not None
            else [_scan(text) for text in texts]
        )
        for text, (spans, typedefs) in zip(texts, scanned, strict=True):
            self.spans[text] = [(start, end) for start, end in spans]
            self.typedefs.update(typedefs)
        shared = (sorted(self.names), sorted(self.aliases), self.typedefs)
        found = (
            _fan_out(host, cache_root, _prototype_job, shared, texts)
            if host is not None and cache_root is not None
            else [_prototypes(self, text) for text in texts]
        )
        for rows in found:
            for name, prototype in rows:
                self.prototypes.setdefault(name, prototype)

    @classmethod
    def _worker(cls, names: list[str], aliases: list[str], typedefs: dict[str, str]) -> FunctionDeclarations:
        row = cls({}, {})
        row.names, row.aliases, row.typedefs = set(names), set(aliases), dict(typedefs)
        return row

    def prepare(self, texts: Iterable[str], host: Host | None, cache_root: Path | None) -> None:
        """Rewrite every text in the pool, cached by its text and these contracts; a refusal is left to rewrite."""
        todo = [t for t in dict.fromkeys(texts) if t not in self.rewritten]
        if host is None or cache_root is None or not self.names or len(todo) < 2:
            return
        shared = (sorted(self.names), sorted(self.aliases), self.typedefs, self.prototypes, self.records)
        for text, after in zip(todo, _fan_out(host, cache_root, _rewrite_job, shared, todo), strict=True):
            if after is not None:
                self.rewritten[text] = after

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
    project: Project, contents: Mapping[Path, str], *, texts: tuple[str, ...] = (), host: Host | None = None
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
    return FunctionDeclarations(
        {"function_symbols": names, "functions": records}, contents, host=host, cache_root=project.cache
    )


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
        contracts = project_declarations(project, contents, texts=(text,), host=host)
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
