"""Plan source token edits for resolved types and their measured member names."""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from functools import lru_cache
from pathlib import Path
from typing import Any

from pycparser import c_ast, c_lexer, c_parser  # type: ignore[import-untyped]

from unbake.layout.structs import Field, Layout, held
from unbake.layout.structs_parser import Parser
from unbake.fold.rewrite_view import View
from unbake.cache import Cache, key
from unbake import atomic as atomic_files


def _gnu_blank(view: str, blank: Any) -> str:
    """Blank GNU extensions pycparser rejects, keeping every offset: attributes,
    label addresses (&&label) and computed gotos (goto *expr)."""
    result, at = [], 0
    for match in re.finditer(r"\b__attribute__\s*\(", view):
        if match.start() < at:
            continue
        depth, end = 0, match.end() - 1
        while end < len(view):
            depth += {"(": 1, ")": -1}.get(view[end], 0)
            end += 1
            if depth == 0:
                break
        result.append(view[at : match.start()] + blank(re.match(r"(?s).*", view[match.start() : end])))
        at = end
    view = "".join(result) + view[at:]
    view = re.sub(r"\b(?:__inline__|__inline|__extension__|__restrict__|__restrict)\b", blank, view)
    view = re.sub(r"([{,(=]\s*)&&(?=\s*[A-Za-z_])", r"\1 &", view)
    return re.sub(r"\bgoto\s*\*", blank, view)


@lru_cache(maxsize=8)
def _context(prefix: str, cache_root: Path | None = None) -> tuple[list[Any], dict[str, bool]]:
    """Parse the shared typed headers once; sources reuse their declarations and typedef scope."""
    if cache_root is None:
        return _parse_context(prefix)
    identity = key(
        "rewrite-context-v1",
        prefix,
        Path(__file__),
        Path(c_parser.__file__),
        Path(c_lexer.__file__),
        Path(c_ast.__file__),
    )
    computed = None

    def produce(output: Path) -> None:
        nonlocal computed
        computed = _parse_context(prefix)
        declarations, scope = computed
        with atomic_files.stream(output, "w") as stream:
            json.dump({"declarations": _encode(declarations), "scope": scope}, stream)

    artifact = Cache(cache_root).produce("rewrite-context", identity, produce)
    if computed is not None:
        return computed
    with artifact.open() as stream:
        document = json.load(stream)
    return _decode(document["declarations"]), document["scope"]


def _encode(value: Any) -> Any:
    records: list[Any] = []
    seen: dict[int, int] = {}

    def visit(item: Any) -> Any:
        if isinstance(item, c_ast.Node):
            index = seen.get(id(item))
            if index is None:
                index = len(records)
                seen[id(item)] = index
                records.append(None)
                records[index] = [
                    type(item).__name__,
                    [visit(getattr(item, slot)) for slot in item.__slots__ if slot != "__weakref__"],
                ]
            return {"ref": index}
        if isinstance(item, c_parser.Coord):
            return {"coord": [item.file, item.line, item.column]}
        if isinstance(item, list):
            return [visit(child) for child in item]
        return item

    root = visit(value)
    return {"root": root, "records": records}


def _decode(value: Any) -> Any:
    objects: list[Any] = []
    for name, _ in value["records"]:
        cls = getattr(c_ast, name)
        if not isinstance(cls, type) or not issubclass(cls, c_ast.Node):
            raise ValueError("rewrite context: expected C declaration node")
        objects.append(object.__new__(cls))

    def visit(item: Any) -> Any:
        if isinstance(item, list):
            return [visit(child) for child in item]
        if isinstance(item, dict):
            if "coord" in item:
                return c_parser.Coord(*item["coord"])
            return objects[item["ref"]]
        return item

    for (_, values), instance in zip(value["records"], objects, strict=True):
        slots = (slot for slot in instance.__slots__ if slot != "__weakref__")
        for slot, item in zip(slots, values, strict=True):
            setattr(instance, slot, visit(item))
    return visit(value["root"])


def _parse_context(prefix: str) -> tuple[list[Any], dict[str, bool]]:
    parser = c_parser.CParser()
    tree = parser.parse(prefix)
    return list(tree.ext), dict(parser._scope_stack[0])


class _SourceParser(c_parser.CParser):  # type: ignore[misc]
    """Represent GNU statement expressions as scoped compound expression nodes."""

    def _parse_assignment_expression(self) -> Any:
        # pycparser's assignment-level GNU shortcut returns before consuming
        # postfix operators. Parse compounds at the primary-expression boundary.
        node = self._parse_conditional_expression()
        if self._is_assignment_op():
            operator = self._advance().value
            right = self._parse_assignment_expression()
            return c_ast.Assignment(operator, node, right, node.coord)
        return node

    def _parse_primary_expression(self) -> Any:
        if self._peek_type() == "LPAREN" and self._peek_type(2) == "LBRACE":
            self._advance()
            node = self._parse_compound_statement()
            self._expect("RPAREN")
            return node
        return super()._parse_primary_expression()


def _parse(prefix: str, view: str, cache_root: Path | None = None) -> c_ast.FileAST:
    """Parse VIEW after PREFIX with source coordinates as if both were one text."""
    declarations, scope = _context(prefix, cache_root)
    parser = _SourceParser()
    parser._scope_stack = [dict(scope)]
    parser.clex.input("\n" * prefix.count("\n") + view, "")
    parser._tokens = c_parser._TokenStream(parser.clex)
    try:
        tree = parser._parse_translation_unit_or_empty()
        token = parser._peek()
        if token is not None:
            parser._parse_error(f"before: {token.value}", parser._tok_coord(token))
    except c_parser.ParseError as error:
        # Some pycparser productions omit coordinates. Its buffered token stream
        # still identifies the current token (or the last consumed token at EOF).
        if not re.search(r":\d+:\d+:", str(error)):
            token = parser._peek()
            if token is None and parser._tokens._index:
                token = parser._tokens._buffer[parser._tokens._index - 1]
            if token is not None:
                raise c_parser.ParseError(f"{parser._tok_coord(token)}: {str(error).lstrip(': ')}") from error
        raise
    return c_ast.FileAST([*declarations, *tree.ext])


def edits(
    parser: Parser,
    context: str | Callable[[], str],
    resolution: dict[str, tuple[str, Layout]],
    tag_only: set[str] | None = None,
    *,
    cache_root: Path | None = None,
    typedef_renames: dict[str, str] | None = None,
    preprocess: Callable[[], View] | None = None,
    source_path: Path | None = None,
    source_line_offset: int = 0,
    source_text: str | None = None,
) -> dict[tuple[int, int], str]:
    """Use C namespaces and expression types; preserve comments, strings and value identifiers."""
    records = {record.name: record for record in (parser.layout(item) for item in parser.aggregates if item.name)}
    bare_tags = tag_only or set()
    changed = any(
        name != target
        or target in bare_tags
        or (records.get(name) is not None and records[name].fields != evidence.fields)
        for name, (target, evidence) in resolution.items()
    )
    if not changed and not typedef_renames:
        return {}

    def blank(match: re.Match[str]) -> str:
        return "".join("\n" if char == "\n" else " " for char in match[0])

    view = re.sub(
        r'/\*.*?\*/|//[^\n]*|"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'',
        lambda match: blank(match) if match[0].startswith(("/*", "//")) else match[0],
        parser.source,
        flags=re.S,
    )
    view = re.sub(r"^[ \t]*#(?:[^\n]*\\\n)*[^\n]*", blank, view, flags=re.M)
    prefix = (context() if callable(context) else context).rstrip() + "\n"
    expanded = preprocess() if preprocess is not None else None
    if expanded is not None:
        view = expanded.text
    view = _gnu_blank(view, blank)
    from unbake.cache import memo

    selection = (
        parser.source,
        prefix,
        view,
        expanded,
        tuple(sorted(records.items())),
        tuple(sorted(resolution.items())),
        frozenset(bare_tags),
        cache_root,
        tuple(sorted((typedef_renames or {}).items())),
        source_path,
        source_line_offset,
        source_text,
    )
    return dict(
        memo(
            "rewrite.plans",
            selection,
            lambda: _plan(
                parser,
                prefix,
                view,
                expanded,
                records,
                resolution,
                bare_tags,
                cache_root,
                typedef_renames,
                source_path,
                source_line_offset,
                source_text,
            ),
            keep=1,
        )
    )


def _plan(
    parser: Parser,
    prefix: str,
    view: str,
    expanded: View | None,
    records: dict[str, Layout],
    resolution: dict[str, tuple[str, Layout]],
    bare_tags: set[str],
    cache_root: Path | None,
    typedef_renames: dict[str, str] | None,
    source_path: Path | None,
    source_line_offset: int,
    source_text: str | None,
) -> dict[tuple[int, int], str]:
    try:
        tree = _parse(prefix, view, cache_root)
    except c_parser.ParseError as error:
        message = str(error)
        coord = re.search(r":(\d+):(\d+):\s*", message)
        filename = str(source_path or "source.c")
        line, column = 1, 1
        if coord:
            index = int(coord[1]) - prefix.count("\n") - 1
            column = int(coord[2])
            if expanded is not None and 0 <= index < len(expanded.locations):
                location = expanded.locations[index]
                filename, line, column = location.file, location.line, location.column
                if filename == str(source_path):
                    line = max(1, line - source_line_offset)
            elif index >= 0:
                line = max(1, index + 1 - source_line_offset)
            message = message[coord.end() :]
        else:
            message = message.lstrip(": ")
        held("source types", f"cannot rewrite resolved layouts: {filename}:{line}:{column}: {message}")
    first_line = prefix.count("\n") + 1
    starts = [0]
    starts.extend(match.end() for match in re.finditer("\n", parser.source))
    # Macro replacement lists have editable spellings too, although the layout
    # parser deliberately excludes directives from its declaration token stream.
    from unbake.layout.structs_parser import _TOKEN

    tokens = {
        token.start(): token
        for token in _TOKEN.finditer(source_text if source_text is not None else parser.source)
        if not token[0].startswith(("/*", "//"))
    }
    replacements: dict[tuple[int, int], str] = {}
    expanded_edits: dict[int, str] = {}
    spellings = expanded.text.splitlines() if expanded is not None else []
    aliases: dict[str, Any] = {}
    tags: dict[tuple[str, str], Any] = {}
    scopes: list[dict[str, Any]] = [{}]
    members: dict[int, dict[str, str]] = {}

    def position(node: Any) -> int | None:
        if node.coord is None or node.coord.line < first_line:
            return None
        line = node.coord.line - first_line
        if expanded is not None:
            return expanded.origins[line] if line < len(expanded.origins) else None
        return starts[line] + node.coord.column - 1 if line < len(starts) else None

    def replace(node: Any, original: str, target: str, *, tag: bool = False) -> None:
        if original == target:
            return
        at = position(node)
        index = node.coord.line - first_line if node.coord is not None else -1
        if expanded is not None and 0 <= index < len(expanded.origins):
            if tag and spellings[index] != original:
                index += 1
            elif isinstance(node, (c_ast.Decl, c_ast.Typedef)) and spellings[index] in ("*", "("):
                index = next((i for i in range(index + 1, len(spellings)) if spellings[i] == original), index)
            at = expanded.origins[index] if index < len(expanded.origins) else None
        if at is None:
            if node.coord is not None and node.coord.line >= first_line:
                held(original, "resolved type token has no editable source provenance")
            return
        token = tokens.get(at)
        if expanded is None:
            if tag and (token is None or token[0] != original):
                token = next((value for start, value in tokens.items() if start > at), None)
            if isinstance(node, (c_ast.Decl, c_ast.Typedef)) and token is not None and token[0] in ("*", "("):
                token = next((value for start, value in tokens.items() if start > at and value[0] == original), None)
        if token is None or token[0] != original:
            held(original, "resolved type token has no editable source provenance")
        span = (token.start(), token.end())
        if span in replacements and replacements[span] != target:
            held(original, "macro spelling requires incompatible resolved type edits")
        replacements[span] = target
        expanded_edits[index] = target

    def resolve(node: Any) -> Any:
        seen = set()
        while isinstance(node, c_ast.TypeDecl):
            node = node.type
            if isinstance(node, c_ast.IdentifierType):
                name = " ".join(node.names)
                if name in aliases and name not in seen:
                    seen.add(name)
                    node = aliases[name]
        return node

    def aggregate(node: Any) -> Any:
        node = resolve(node)
        if isinstance(node, (c_ast.Struct, c_ast.Union)) and not node.decls:
            return tags.get((type(node).__name__, node.name), node)
        return node

    def expression(node: Any) -> Any:
        if isinstance(node, c_ast.Compound) and node.block_items:
            scopes.append(
                {item.name: item.type for item in node.block_items if isinstance(item, c_ast.Decl) and item.name}
            )
            try:
                return expression(node.block_items[-1])
            finally:
                scopes.pop()
        if isinstance(node, c_ast.ID):
            return next((scope[node.name] for scope in reversed(scopes) if node.name in scope), None)
        if isinstance(node, c_ast.Cast):
            return node.to_type.type
        if isinstance(node, c_ast.ArrayRef):
            base = resolve(expression(node.name))
            return base.type if isinstance(base, (c_ast.PtrDecl, c_ast.ArrayDecl)) else None
        if isinstance(node, c_ast.UnaryOp):
            base = resolve(expression(node.expr))
            if node.op == "*":
                return base.type if isinstance(base, (c_ast.PtrDecl, c_ast.ArrayDecl)) else None
            if node.op == "&":
                return c_ast.PtrDecl([], base)
            return base
        if isinstance(node, c_ast.StructRef):
            base = owner(node)
            return (
                next((member.type for member in base.decls or [] if member.name == node.field.name), None)
                if isinstance(base, (c_ast.Struct, c_ast.Union))
                else None
            )
        if isinstance(node, c_ast.FuncCall):
            base = resolve(expression(node.name))
            if isinstance(base, c_ast.PtrDecl):
                base = resolve(base.type)
            return base.type if isinstance(base, c_ast.FuncDecl) else None
        if isinstance(node, c_ast.BinaryOp) and node.op in ("+", "-"):
            left, right = expression(node.left), expression(node.right)
            if isinstance(resolve(left), (c_ast.PtrDecl, c_ast.ArrayDecl)):
                return left
            if node.op == "+" and isinstance(resolve(right), (c_ast.PtrDecl, c_ast.ArrayDecl)):
                return right
        if isinstance(node, c_ast.Assignment):
            return expression(node.lvalue)
        if isinstance(node, c_ast.TernaryOp):
            return expression(node.iftrue) or expression(node.iffalse)
        if isinstance(node, c_ast.ExprList) and node.exprs:
            return expression(node.exprs[-1])
        return None

    def owner(node: Any) -> Any:
        base = resolve(expression(node.name))
        if node.type == "->" and isinstance(base, (c_ast.PtrDecl, c_ast.ArrayDecl)):
            base = resolve(base.type)
        return aggregate(base)

    def field_names(node: Any, fields: tuple[Field, ...], target: tuple[Field, ...]) -> None:
        if not isinstance(node, (c_ast.Struct, c_ast.Union)) or not node.decls:
            return
        members[id(node)] = {old.name: new.name for old, new in zip(fields, target, strict=True)}
        for declaration, old, new in zip(node.decls, fields, target, strict=True):
            replace(declaration, old.name, new.name)
            nested = resolve(declaration.type)
            while isinstance(nested, c_ast.ArrayDecl):
                nested = resolve(nested.type)
            if old.fields and id(nested) not in members:
                # A named member type already has its own resolved field map.
                # Its enclosing layout may still carry the original member names.
                field_names(nested, old.fields, new.fields)

    class Rewrite(c_ast.NodeVisitor):  # type: ignore[misc]
        def visit_Typedef(self, node: Any) -> None:
            aliases[node.name] = node.type
            if node.name in (typedef_renames or {}):
                replace(node, node.name, (typedef_renames or {})[node.name])
            elif node.name in resolution:
                replace(node, node.name, resolution[node.name][0])
                # An anonymous aggregate is recorded under its typedef name.
                inner = node.type.type if isinstance(node.type, c_ast.TypeDecl) else None
                if (
                    isinstance(inner, (c_ast.Struct, c_ast.Union))
                    and not inner.name
                    and inner.decls
                    and node.name in records
                    and position(node) is not None
                ):
                    field_names(inner, records[node.name].fields, resolution[node.name][1].fields)
            self.generic_visit(node)

        def visit_IdentifierType(self, node: Any) -> None:
            if len(node.names) == 1 and node.names[0] in (typedef_renames or {}):
                replace(node, node.names[0], (typedef_renames or {})[node.names[0]])
            elif len(node.names) == 1 and node.names[0] in resolution:
                name = node.names[0]
                target, evidence = resolution[name]
                spelling = f"{evidence.kind} {target}" if target in bare_tags else target
                replace(node, name, spelling)

        def visit_Struct(self, node: Any) -> None:
            if node.name and node.decls:
                tags[(type(node).__name__, node.name)] = node
                if position(node) is not None and node.name in resolution:
                    field_names(node, records[node.name].fields, resolution[node.name][1].fields)
            if node.name in resolution:
                replace(node, node.name, resolution[node.name][0], tag=True)
            scopes.append({})
            self.generic_visit(node)
            scopes.pop()

        visit_Union = visit_Struct

        def visit_Decl(self, node: Any) -> None:
            if node.name:
                scopes[-1][node.name] = node.type
            self.generic_visit(node)

        def visit_Compound(self, node: Any) -> None:
            scopes.append({})
            self.generic_visit(node)
            scopes.pop()

        def visit_FuncDef(self, node: Any) -> None:
            self.visit(node.decl)
            params = node.decl.type.args.params if node.decl.type.args is not None else []
            scopes.append({param.name: param.type for param in params if isinstance(param, c_ast.Decl) and param.name})
            for declaration in node.param_decls or []:
                self.visit(declaration)
            self.visit(node.body)
            scopes.pop()

        def visit_FuncDecl(self, node: Any) -> None:
            scopes.append({})
            self.generic_visit(node)
            scopes.pop()

        def visit_StructRef(self, node: Any) -> None:
            base = owner(node)
            target = members.get(id(base), {}).get(node.field.name)
            if target:
                replace(node.field, node.field.name, target)
            self.generic_visit(node)

    # Shared declarations only seed namespaces. Their coordinates precede the
    # editable source, so resolutions cannot produce edits in this part.
    from unbake.cache import memo

    shared = tree.ext[: len(_context(prefix, cache_root)[0])]

    def seed() -> tuple[dict[str, Any], dict[tuple[str, str], Any], dict[str, Any]]:
        Rewrite().visit(c_ast.FileAST(shared))
        return dict(aliases), dict(tags), dict(scopes[0])

    initial_aliases, initial_tags, initial_scope = memo("rewrite.namespaces", prefix, seed, keep=8)
    aliases.update(initial_aliases)
    tags.update(initial_tags)
    scopes[0].update(initial_scope)
    Rewrite().visit(c_ast.FileAST(tree.ext[len(shared) :]))
    if expanded is not None:
        for index, at in enumerate(expanded.origins):
            token = tokens.get(at) if at is not None else None
            if (
                token is not None
                and (target := replacements.get((token.start(), token.end()))) is not None
                and expanded_edits.get(index, token[0]) != target
            ):
                held(token[0], "macro spelling also supplies an unchanged or differently typed token")
    return replacements
