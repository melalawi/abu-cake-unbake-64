"""Plan source token edits for resolved types and their measured member names."""

from __future__ import annotations

import re
from functools import lru_cache
from typing import Any

from pycparser import c_ast, c_parser  # type: ignore[import-untyped]

from unbake.layout.structs import Field, Layout, held
from unbake.layout.structs_parser import Parser


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
    view = re.sub(r"([{,(=]\s*)&&(?=\s*[A-Za-z_])", r"\1 &", view)
    return re.sub(r"\bgoto\s*\*", blank, view)


@lru_cache(maxsize=8)
def _context(prefix: str) -> tuple[list[Any], dict[str, bool]]:
    """Parse the shared typed headers once; sources reuse their declarations and typedef scope."""
    parser = c_parser.CParser()
    tree = parser.parse(prefix)
    return list(tree.ext), dict(parser._scope_stack[0])


def _parse(prefix: str, view: str) -> c_ast.FileAST:
    """Parse VIEW after PREFIX with source coordinates as if both were one text."""
    declarations, scope = _context(prefix)
    parser = c_parser.CParser()
    parser._scope_stack = [dict(scope)]
    parser.clex.input("\n" * prefix.count("\n") + view, "")
    parser._tokens = c_parser._TokenStream(parser.clex)
    tree = parser._parse_translation_unit_or_empty()
    token = parser._peek()
    if token is not None:
        parser._parse_error(f"before: {token.value}", parser._tok_coord(token))
    return c_ast.FileAST([*declarations, *tree.ext])


def edits(
    parser: Parser, context: str, resolution: dict[str, tuple[str, Layout]], tag_only: set[str] | None = None
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
    if not changed:
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
    view = _gnu_blank(view, blank)
    prefix = context.rstrip() + "\n"
    try:
        tree = _parse(prefix, view)
    except c_parser.ParseError as error:
        held("source types", f"cannot rewrite resolved layouts: {error}")
    first_line = prefix.count("\n") + 1
    starts = [0]
    starts.extend(match.end() for match in re.finditer("\n", parser.source))
    tokens = {token.start(): token for token in parser.tokens}
    replacements: dict[tuple[int, int], str] = {}
    aliases: dict[str, Any] = {}
    tags: dict[tuple[str, str], Any] = {}
    scopes: list[dict[str, Any]] = [{}]
    members: dict[int, dict[str, str]] = {}

    def position(node: Any) -> int | None:
        if node.coord is None or node.coord.line < first_line:
            return None
        line = node.coord.line - first_line
        return starts[line] + node.coord.column - 1 if line < len(starts) else None

    def replace(node: Any, original: str, target: str, *, tag: bool = False) -> None:
        at = position(node)
        if at is None or original == target:
            return
        token = tokens.get(at)
        if tag and (token is None or token[0] != original):
            token = next((value for start, value in tokens.items() if start > at), None)
        if isinstance(node, (c_ast.Decl, c_ast.Typedef)) and token is not None and token[0] in ("*", "("):
            token = next((value for start, value in tokens.items() if start > at and value[0] == original), None)
        if token is None or token[0] != original:
            held(original, "resolved type token has no editable source provenance")
        replacements[(token.start(), token.end())] = target

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
            if node.name in resolution:
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
            if len(node.names) == 1 and node.names[0] in resolution:
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

    Rewrite().visit(tree)
    return replacements
