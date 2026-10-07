"""Compare full declarations through their own guarded alias and tag providers."""

from __future__ import annotations

import operator
from collections.abc import Callable
from pathlib import Path
from typing import Any

from pycparser import c_ast  # type: ignore[import-untyped]

from unbake import cdecl
from unbake.project.headers import Graph
from unbake.typemap.declarations import canonical


class Unproved(Exception):
    pass


class Identity:
    """One plan's lazy declaration parses; no I/O or namespace-wide alias merging."""

    def __init__(self, contents: dict[Path, str], catalogs: dict[Path, Any], roots: tuple[Path, ...]):
        self.contents, self.catalogs = contents, catalogs
        self.graph = Graph.contents(contents, roots)
        self.names = {name for catalog in catalogs.values() for name in catalog.typedefs}
        self.nodes: dict[str, Any] = {}
        self.scopes: dict[Path, tuple[Path, ...]] = {}
        self.results: dict[tuple[str, str, Path, Path], bool] = {}

    def parse(self, text: str) -> Any:
        if text not in self.nodes:
            try:
                self.nodes[text] = cdecl.parse(text, typedefs=self.names).ext[0].type
            except Exception as error:
                raise Unproved from error
        return self.nodes[text]

    def scope(self, path: Path) -> tuple[Path, ...]:
        if path not in self.scopes:
            self.scopes[path] = (path, *sorted(set(self.graph.closure((path,)).paths) - {path}))
        return self.scopes[path]

    def providers(self, path: Path, namespace: str, name: str) -> list[Path]:
        def has(home: Path) -> bool:
            catalog = self.catalogs[home]
            return name in (catalog.typedefs if namespace == "alias" else catalog.tags)

        homes = [path] if has(path) else [home for home in self.scope(path) if has(home)]
        if any(not self.catalogs[home].guard for home in homes):
            raise Unproved
        return homes

    def alias(self, path: Path, name: str, active: tuple[tuple[str, str], ...] = ()) -> object:
        alias_key = ("alias", name)
        if alias_key in active:
            for kind in ("struct", "union"):
                if (kind, name) in active:
                    return ("recursive", kind, name)
            raise Unproved
        homes = self.providers(path, "alias", name)
        if not homes:
            # Opaque identifiers cannot certify an anonymous/tagged layout.
            raise Unproved
        values = []
        for home in homes:
            start, end, _ = self.catalogs[home].typedefs[name]
            text = cdecl.declaration_source(self.contents[home][start:end])
            values.append(self.shape(self.parse(text), home, (*active, alias_key), anonymous=name))
        if any(value != values[0] for value in values[1:]):
            raise Unproved
        return values[0]

    def tag(self, path: Path, kind: str, name: str, active: tuple[tuple[str, str], ...]) -> object:
        homes = self.providers(path, "tag", name)
        if not homes:
            return ("opaque", kind, name)
        values = []
        for home in homes:
            declared_kind, start, end, _ = self.catalogs[home].tags[name]
            if declared_kind != kind:
                raise Unproved
            text = kind + " " + name + " " + cdecl.declaration_source(self.contents[home][start:end]) + ";"
            values.append(self.shape(self.parse(text), home, active))
        if any(value != values[0] for value in values[1:]):
            raise Unproved
        return values[0]

    def constant(self, node: Any) -> int:
        if isinstance(node, c_ast.Constant) and "int" in node.type:
            text = node.value.rstrip("uUlL")
            return int(text, 8 if len(text) > 1 and text.startswith("0") and text.isdigit() else 0)
        if isinstance(node, c_ast.UnaryOp) and node.op in ("+", "-", "~"):
            unary: dict[str, Callable[[int], int]] = {"+": operator.pos, "-": operator.neg, "~": operator.invert}
            return unary[node.op](self.constant(node.expr))
        if isinstance(node, c_ast.BinaryOp):
            operations: dict[str, Callable[[int, int], int]] = {
                "+": operator.add,
                "-": operator.sub,
                "*": operator.mul,
                "<<": operator.lshift,
                ">>": operator.rshift,
                "&": operator.and_,
                "|": operator.or_,
                "^": operator.xor,
            }
            if node.op in operations:
                return operations[node.op](self.constant(node.left), self.constant(node.right))
        raise Unproved

    def shape(self, node: Any, path: Path, active: tuple[tuple[str, str], ...], *, anonymous: str = "") -> object:
        if isinstance(node, c_ast.TypeDecl):
            value = self.shape(node.type, path, active, anonymous=anonymous)
            return ("qualified", tuple(node.quals), value) if node.quals else value
        if isinstance(node, c_ast.IdentifierType):
            name = " ".join(node.names)
            if name in self.names:
                return self.alias(path, name, active)
            if set(node.names) <= {
                "void",
                "char",
                "signed",
                "unsigned",
                "short",
                "int",
                "long",
                "float",
                "double",
                "_Bool",
            }:
                return ("scalar", canonical(name, {}))
            raise Unproved
        if isinstance(node, (c_ast.Struct, c_ast.Union)):
            kind = type(node).__name__.lower()
            name = node.name or anonymous
            key = (kind, name)
            if node.decls is None:
                if key in active:
                    return ("recursive", kind, name)
                return self.tag(path, kind, name, active)
            if key in active:
                return ("recursive", kind, name)
            nested = (*active, key)
            return (
                "aggregate",
                kind,
                name,
                tuple(
                    (
                        member.name,
                        self.shape(member.type, path, nested),
                        None if member.bitsize is None else self.constant(member.bitsize),
                    )
                    for member in node.decls
                ),
            )
        if isinstance(node, c_ast.PtrDecl):
            return ("pointer", tuple(node.quals), self.shape(node.type, path, active))
        if isinstance(node, c_ast.ArrayDecl):
            return (
                "array",
                tuple(node.dim_quals),
                None if node.dim is None else self.constant(node.dim),
                self.shape(node.type, path, active),
            )
        if isinstance(node, c_ast.FuncDecl):
            params = (
                ()
                if node.args is None
                else tuple(
                    ("variadic",) if isinstance(param, c_ast.EllipsisParam) else self.shape(param.type, path, active)
                    for param in node.args.params
                )
            )
            return ("function", self.shape(node.type, path, active), params)
        raise Unproved

    def equal(self, namespace: str, name: str, left: Path, right: Path) -> bool:
        key = namespace, name, left, right
        if key not in self.results:
            try:
                if namespace == "alias":
                    a, b = self.alias(left, name), self.alias(right, name)
                else:
                    kind = self.catalogs[left].tags[name][0]
                    a, b = self.tag(left, kind, name, ()), self.tag(right, kind, name, ())
                self.results[key] = a == b
            except (Unproved, ArithmeticError, ValueError):
                self.results[key] = False
        return self.results[key]
