"""Strip equal local typedefs/externs and refuse conflicting imported declarations."""

from __future__ import annotations

import re
from pathlib import Path

from pycparser import c_ast, c_generator  # type: ignore[import-untyped]

from unbake import cache as retention
from unbake import cdecl
from unbake.cdecl import declaration_source, declarations
from unbake.config import Held
from unbake.process import capture
from unbake.process import named as cause_named
from unbake.typemap.declarations import _type, canonical
from unbake.typemap.header_names import type_identity


def spans(text: str) -> list[tuple[int, int]]:
    source = declaration_source(text)
    result = []
    start = 0
    depth = 0
    for token in re.finditer(r'"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'|[{};]', source):
        value = token[0]
        if value == "{":
            depth += 1
        elif value == "}":
            depth -= 1
            if depth == 0 and not re.match(r"\s*(?:typedef|extern)\b", source[start : token.start()]):
                start = token.end()
        elif value == ";" and depth == 0:
            match = re.match(r"\s*(typedef|extern)\b", source[start : token.end()])
            if match:
                result.append((start + match.start(1), token.end()))
            else:
                statement = source[start : token.end()]
                # A file-scope function prototype has external linkage even
                # when its author omits the extern storage specifier.
                if "(" in statement and "=" not in statement and not re.search(r"\bstatic\b", statement):
                    row = declarations(statement)
                    if any(re.search(r"\b" + re.escape(name) + r"\s*\(", statement) for name in row.declared):
                        beginning = re.search(r"\S", statement)
                        assert beginning is not None
                        result.append((start + beginning.start(), token.end()))
            start = token.end()
    return result


def normalized(text: str) -> str:
    return re.sub(r"\s+", "", text)


def _primitive_types(tree: c_ast.Node) -> None:
    """Normalize builtin synonyms before constructing derived declarator types."""

    class Types(c_ast.NodeVisitor):  # type: ignore[misc]
        def visit_IdentifierType(self, node: object) -> None:
            node.names = canonical(" ".join(node.names), {}).split()  # type: ignore[attr-defined]

    Types().visit(tree)


class _AliasTypes(dict[str, str]):
    """Typedefs and aggregate definitions in their separate C namespaces."""

    def __init__(self) -> None:
        super().__init__()
        self.aggregates: dict[str, str] = {}


@retention.memoized("layout.redeclarations._aliases", size=retention.memory_size, copy_out=retention.clone)
def _aliases(text: str) -> _AliasTypes:
    rows = [text[start:end] for start, end in spans(text) if re.match(r"typedef\b", text[start:end])]
    names = {name for row in rows for name in declarations(row).typedefs}
    result = _AliasTypes()
    masked = declaration_source(text)
    for tag, bodies in tag_definitions(text).items():
        for begin, end in bodies:
            kind = re.search(r"\b(struct|union|enum)\s+" + re.escape(tag) + r"\s*$", masked[:begin])
            if kind:
                result.aggregates[kind[1] + " " + tag] = kind[1] + " " + tag + " " + masked[begin:end]
    for row in rows:
        masked = declaration_source(row)
        if "{" in masked:
            named = re.match(r"typedef\s+(?:struct|union|enum)\s+\w+\s*\{", masked)
            if named is not None:
                begin = named.end() - 1
                depth = 0
                for token in re.finditer(r'"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'|[{}]', masked[begin:]):
                    depth += (token[0] == "{") - (token[0] == "}")
                    if depth == 0 and token[0] == "}":
                        row = row[:begin] + row[begin + token.end() :]
                        break
        try:
            tree = cdecl.parse(declaration_source(row), typedefs=names)
        except Exception:
            continue
        _primitive_types(tree)
        for node in tree.ext:
            if isinstance(node, c_ast.Typedef):
                result[node.name] = _type(node.type)
    return result


def aliases(texts: list[str]) -> dict[str, str]:
    """Read typedefs and preserve layouts without expanding recursive tag spellings."""
    result = _AliasTypes()
    for text in texts:
        row = _aliases(text)
        result.update(row)
        result.aggregates.update(row.aggregates)
    return result


@retention.memoized("layout.redeclarations._signature", size=retention.memory_size, copy_out=retention.clone)
def _signature(text: str, items: tuple[tuple[str, str], ...], aggregate_items: tuple[tuple[str, str], ...]) -> object:
    mapping = dict(items)
    aggregates = {**dict(aggregate_items), **_aliases(text).aggregates}
    row = declarations(text)
    scope = dict.fromkeys(row.uses | row.typedefs | mapping.keys(), True)
    try:
        tree = cdecl.parse(declaration_source(text), typedefs=scope)
    except Exception:
        # Unsupported compiler syntax is equal only when its bytes agree.
        return normalized(text)
    result = []
    try:
        for node in tree.ext:
            if isinstance(node, (c_ast.Decl, c_ast.Typedef)):
                kind = "typedef" if isinstance(node, c_ast.Typedef) else "extern"
                result.append((kind, node.name, type_identity(_type(node.type), mapping, aggregates=aggregates)))
            else:
                return normalized(text)
    except Held:
        return normalized(text)
    return tuple(result)


def equivalent(left: str, right: str, mapping: dict[str, str]) -> bool:
    """Compare declarator types, ignoring parameter names and extern spelling.

    An empty parameter list `f()` is compatible with `f(void)`; it is never equivalent to any other list."""
    items = tuple(sorted(mapping.items()))
    aggregates = tuple(sorted(getattr(mapping, "aggregates", {}).items()))
    return _signature(left, items, aggregates) == _signature(right, items, aggregates)


_DECLARATOR = re.compile(r"\b([A-Za-z_]\w*)\s*(?:\)\s*)*[\[(;=,]")
_KEYWORDS = frozenset(
    [
        "auto",
        "char",
        "const",
        "double",
        "enum",
        "extern",
        "float",
        "inline",
        "int",
        "long",
        "register",
        "short",
        "signed",
        "static",
        "struct",
        "typedef",
        "union",
        "unsigned",
        "void",
        "volatile",
    ]
)


def parse(source: Path, variant: str) -> cdecl.Declarations:
    """One version branch of a source's file-scope declaration; a refusal names the source and the symbol."""
    try:
        return declarations(variant)
    except Held as error:
        names = [m[1] for m in _DECLARATOR.finditer(variant) if m[1] not in _KEYWORDS]
        symbol = names[0] if names else variant.strip().splitlines()[0]
        detail = error.reason.split(":", 1)[-1].strip()
        raise Held(
            capture(
                error,
                cause=cause_named(
                    f"{error.key}",
                    f"{error.key}: {source}: {symbol}: {detail}",
                    owner="layout.redeclarations",
                    stage=error.phase,
                ),
            )
        ) from error


def declared(source: Path, text: str) -> dict[str, int]:
    """Each name SOURCE declares at file scope, in any version branch, with the offset of its first declaration."""
    first: dict[str, int] = {}
    for start, end in spans(text):
        for variant in variants(text[start:end]):
            for name in parse(source, variant).declared:
                first.setdefault(name, start)
    return first


def variants(text: str) -> tuple[str, ...]:
    """Expand declaration-local conditionals without interpreting project macros."""
    lines = text.splitlines(keepends=True)
    begin = next((i for i, line in enumerate(lines) if re.match(r"\s*#\s*(?:if|ifdef|ifndef)\b", line)), None)
    if begin is None:
        return (text,)
    depth = 0
    branches = []
    start = begin + 1
    for position in range(begin, len(lines)):
        directive = re.match(r"\s*#\s*(if|ifdef|ifndef|elif|else|endif)\b", lines[position])
        if directive is None:
            continue
        kind = directive[1]
        if kind in ("if", "ifdef", "ifndef"):
            depth += 1
        elif kind == "endif":
            depth -= 1
            if not depth:
                branches.append("".join(lines[start:position]))
                prefix, suffix = "".join(lines[:begin]), "".join(lines[position + 1 :])
                return tuple(result for branch in branches for result in variants(prefix + branch + suffix))
        elif depth == 1:
            branches.append("".join(lines[start:position]))
            start = position + 1
    raise Held(
        cause_named(
            "layout.redeclaration",
            "layout.redeclaration: unclosed declaration conditional",
            owner="layout.redeclarations",
            stage="layout",
        )
    )


@retention.memoized("layout.redeclarations.catalog", size=retention.memory_size, copy_out=retention.clone)
def catalog(header: str) -> dict[str, str]:
    result: dict[str, str] = {}
    mapping = aliases([header])
    for start, end in spans(header):
        declaration = header[start:end]
        for variant in variants(declaration):
            row = declarations(variant)
            for name in row.typedefs | row.declared:
                if name in result and not equivalent(result[name], variant, mapping):
                    raise Held(
                        cause_named(
                            f"layout.redeclaration.{name}",
                            f"layout.redeclaration.{name}: shared conflict\n{result[name]}\n{variant}",
                            owner="layout.redeclarations",
                            stage="layout",
                        )
                    )
                result[name] = variant
    return result


@retention.memoized("layout.redeclarations._tags", size=retention.memory_size, copy_out=retention.clone)
def _tags(text: str) -> frozenset[str]:
    return frozenset(declarations(text).tags)


@retention.memoized("layout.redeclarations.tag_definitions", size=retention.memory_size, copy_out=retention.clone)
def tag_definitions(text: str) -> dict[str, tuple[tuple[int, int], ...]]:
    """Read complete file-scope tags without parsing implementation bodies."""
    masked = declaration_source(text)
    pattern = r'"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'|\b(?:struct|union|enum)\s+(?P<tag>[A-Za-z_]\w*)\s*(?=\{)|[{}]'
    depth = 0
    active = None
    beginning = 0
    result: dict[str, list[tuple[int, int]]] = {}
    for token in re.finditer(pattern, masked):
        if token.group("tag") and depth == 0:
            active = token.group("tag")
        if token[0] == "{" and depth == 0:
            beginning = token.start()
        depth += (token[0] == "{") - (token[0] == "}")
        if token[0] == "}" and depth == 0 and active is not None:
            result.setdefault(active, []).append((beginning, token.end()))
            active = None
    return {name: tuple(rows) for name, rows in result.items()}


def local_tags(text: str) -> set[str]:
    return set(tag_definitions(text))


def _body_signature(body: str, mapping: dict[str, str]) -> str:
    source = "struct DeclarationBody " + declaration_source(body) + ";"
    try:
        names = declarations(source).uses | mapping.keys()
        tree = cdecl.parse(source, typedefs=names)
    except Exception:
        return normalized(declaration_source(body))

    class Types(c_ast.NodeVisitor):  # type: ignore[misc]
        def visit_IdentifierType(self, node: object) -> None:
            node.names = [canonical(" ".join(node.names), mapping)]  # type: ignore[attr-defined]

    Types().visit(tree)
    return normalized(c_generator.CGenerator().visit(tree))


def privatize_tags(text: str, imported: list[str], owner: str) -> tuple[str, dict[str, str]]:
    """Strip equal tag bodies; keep conflicting source aggregate identities private."""
    local = tag_definitions(text)
    if not local:
        return text, {}
    shared = set().union(*(_tags(header) for header in imported))
    collisions = local.keys() & shared
    occupied = set(local) | set(re.findall(r"\b[A-Za-z_]\w*\b", declaration_source(text))) | shared
    renamed = {}
    edits = []
    mapping = aliases([*imported, text])
    for name in sorted(collisions):
        own = local[name]
        bodies = [(header, start, end) for header in imported for start, end in tag_definitions(header).get(name, ())]
        if len(own) == len(bodies) == 1:
            start, end = own[0]
            header, shared_start, shared_end = bodies[0]
            prefix = r"\b(struct|union|enum)\s+" + re.escape(name) + r"\s*$"
            own_kind = re.search(prefix, declaration_source(text[:start]))
            shared_kind = re.search(prefix, declaration_source(header[:shared_start]))
            if (
                own_kind is not None
                and shared_kind is not None
                and own_kind[1] == shared_kind[1]
                and _body_signature(text[start:end], mapping)
                == _body_signature(header[shared_start:shared_end], mapping)
            ):
                edits.append((start, end, ""))
                continue
        stem = name + "_" + owner
        target = stem
        ordinal = 2
        while target in occupied:
            target = stem + "_" + str(ordinal)
            ordinal += 1
        renamed[name] = target
        occupied.add(target)
    masked = declaration_source(text)
    pattern = r'"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'|\b(?:struct|union|enum)\s+(?P<tag>[A-Za-z_]\w*)'
    for match in re.finditer(pattern, masked):
        name = match.group("tag")
        if name in renamed:
            edits.append((match.start("tag"), match.end("tag"), renamed[name]))
    for start, end, value in sorted(edits, reverse=True):
        text = text[:start] + value + text[end:]
    return text, renamed


def _locals(text: str) -> list[tuple[int, int, str, dict[str, str]]]:
    """Each file-scope declaration of TEXT with the names it declares in any branch (name -> variant).
    A name the source #defines is an alias (often per version) for another symbol: the header's declaration of
    that name does not declare what the local one does after expansion, so it is left out."""
    macros = set(re.findall(r"^[ \t]*#[ \t]*define[ \t]+(\w+)", text, re.M))
    rows = []
    for start, end in spans(text):
        declaration = text[start:end]
        local = {
            name: variant
            for variant in variants(declaration)
            for name in declarations(variant).typedefs | declarations(variant).declared
            if name not in macros
        }
        rows.append((start, end, declaration, local))
    return rows


def uncovered(text: str, imported: list[str]) -> set[str]:
    """Names of local declarations the imported headers declare only in part (a per-version conditional naming one
    shared symbol and one the headers lack): importing their homes too lets `strip` remove the whole declaration."""
    shared = {name for header in imported for name in catalog(header)}
    result: set[str] = set()
    for _, _, _, local in _locals(text):
        if local.keys() & shared:
            result |= local.keys() - shared
    return result


def strip(text: str, imported: list[str], disagreements: dict[str, tuple[str, str]] | None = None) -> str:
    """Remove local declarations the imported headers already make.

    A local declaration whose type differs from the header's is refused, or, given DISAGREEMENTS, removed and
    recorded there as name -> (local, header) so the caller proves the header form against the ROM."""
    text, _ = privatize_tags(text, imported, "local")
    shared: dict[str, str] = {}
    mapping = aliases(imported)
    local_mapping = {**mapping, **aliases([text])}
    for header in imported:
        for name, declaration in catalog(header).items():
            if name in shared and not equivalent(shared[name], declaration, mapping):
                raise Held(
                    cause_named(
                        f"layout.redeclaration.{name}",
                        f"layout.redeclaration.{name}: shared conflict\n{shared[name]}\n{declaration}",
                        owner="layout.redeclarations",
                        stage="layout",
                    )
                )
            shared[name] = declaration
    for start, end, declaration, local in reversed(_locals(text)):
        collisions = local.keys() & shared.keys()
        if not collisions:
            continue
        for name in sorted(collisions):
            shared_declaration = shared[name]
            for left, right in sorted(
                (span for rows in tag_definitions(shared_declaration).values() for span in rows), reverse=True
            ):
                shared_declaration = shared_declaration[:left] + shared_declaration[right:]
            if not equivalent(local[name], shared_declaration, local_mapping):
                if disagreements is None:
                    raise Held(
                        cause_named(
                            f"layout.redeclaration.{name}",
                            f"layout.redeclaration.{name}: local:\n{declaration}\nshared:\n{shared[name]}",
                            owner="layout.redeclarations",
                            stage="layout",
                        )
                    )
                disagreements[name] = (local[name].strip(), shared[name].strip())
        if collisions != local.keys():
            raise Held(
                cause_named(
                    "layout.redeclarations.strip",
                    "layout.redeclaration: partially imported conditional declaration\n" + declaration,
                    owner="layout.redeclarations",
                    stage="layout",
                )
            )
        # Remove declaration bytes only; retain preceding comments and directives.
        masked = declaration_source(declaration)
        match = re.search(r"\b(?:typedef|extern)\b", masked)
        begin = start + match.start() if match is not None else start
        text = text[:begin] + text[end:]
    return text
