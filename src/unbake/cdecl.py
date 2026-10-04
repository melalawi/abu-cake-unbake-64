"""The one place C declaration text is parsed.

- parse(text, typedefs=...) returns a fresh pycparser AST (callers may change it); `typedefs` seeds names a
  preprocessed prefix already declared, so a unit can be parsed without re-reading its headers.
- records(text) returns the aggregate layouts of header text (layout.structs_parser), memoised by text
  (cache kind `decl`); callers get their own list.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from pycparser import c_ast, c_parser  # type: ignore[import-untyped]

ParseError = c_parser.ParseError


class SeededParser(c_parser.CParser):  # type: ignore[misc]
    """Resume the file scope of an exact preprocessed prefix (pinned pycparser 3)."""

    def __init__(self, scope: dict[str, bool]) -> None:
        super().__init__()
        self.scope = scope

    def _parse_translation_unit_or_empty(self) -> Any:
        self._scope_stack = [self.scope.copy()]
        return super()._parse_translation_unit_or_empty()


def parser(typedefs: Iterable[str] | dict[str, bool] = ()) -> c_parser.CParser:
    scope = typedefs if isinstance(typedefs, dict) else dict.fromkeys(typedefs, True)
    return SeededParser(scope) if scope else c_parser.CParser()


def parse(text: str, *, typedefs: Iterable[str] | dict[str, bool] = ()) -> c_ast.FileAST:
    return parser(typedefs).parse(text)


def records(text: str) -> list[Any]:
    from unbake.cache import memo
    from unbake.layout.structs_parser import Parser

    return list(memo("decl.records", text, lambda: Parser(text).parse(), keep=32768))
