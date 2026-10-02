"""Read header declarators without requiring their typedefs to be ordered first."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from unbake.project.config import Held

_IDENTIFIER = re.compile(r"[A-Za-z_]\w*\Z")
_SCALARS = set(["void", "char", "short", "int", "long", "float", "double", "signed", "unsigned", "_Bool", "_Complex"])
_QUALIFIERS = set(["const", "volatile", "restrict", "__restrict", "__restrict__"])
_STORAGE = set(["typedef", "extern", "static", "auto", "register", "inline", "__inline", "__inline__", "__extension__"])


@dataclass
class Declarations:
    typedefs: set[str] = field(default_factory=set)
    uses: set[str] = field(default_factory=set)
    exports: set[str] = field(default_factory=set)


class Parser:
    """Parse declaration specifiers and recursive (including abstract) declarators.

    A typedef name is an identifier in a type position. Its definition is not
    needed to discover dependencies; names in parameters, fields and extents
    belong to different positions and cannot become type providers or uses.
    """

    def __init__(self, source: str) -> None:
        source = re.sub(r"\\\n", "", source)
        source = re.sub(r"/\*.*?\*/|//[^\n]*", " ", source, flags=re.S)
        source = re.sub(r"^\s*#[^\n]*", "", source, flags=re.M)
        self.tokens = re.findall(r'"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'|[A-Za-z_]\w*|\.\.\.|\S', source)
        self.index = 0
        self.result = Declarations()

    def peek(self, offset: int = 0) -> str:
        position = self.index + offset
        return self.tokens[position] if position < len(self.tokens) else ""

    def take(self, expected: str | None = None) -> str:
        token = self.peek()
        if not token or (expected is not None and token != expected):
            raise Held("m2c", f"header declaration: expected {expected or 'token'}, found {token!r}")
        self.index += 1
        return token

    def skip(self, stops: set[str]) -> None:
        pairs = {"(": ")", "[": "]", "{": "}"}
        while self.peek() and self.peek() not in stops:
            token = self.take()
            if token in pairs:
                self.skip({pairs[token]})
                self.take(pairs[token])

    def specifiers(self) -> None:
        while self.peek() in _QUALIFIERS:
            self.take()
        if self.peek() in ("struct", "union", "enum"):
            kind = self.take()
            tag = self.take() if _IDENTIFIER.fullmatch(self.peek()) else ""
            if self.peek() == "{":
                if tag:
                    self.result.exports.add(tag)
                self.take("{")
                if kind == "enum":
                    self.skip({"}"})
                else:
                    while self.peek() and self.peek() != "}":
                        self.declaration()
                self.take("}")
        elif self.peek() in _SCALARS:
            while self.peek() in _SCALARS | _QUALIFIERS:
                self.take()
        else:
            name = self.take()
            if not _IDENTIFIER.fullmatch(name):
                raise Held("m2c", f"header declaration: expected type, found {name!r}")
            self.result.uses.add(name)
        while self.peek() in _QUALIFIERS:
            self.take()

    def declarator(self, *, abstract: bool = False) -> str:
        while self.peek() == "*":
            self.take()
            while self.peek() in _QUALIFIERS:
                self.take()
        name = ""
        # Parentheses group a declarator; suffix parentheses contain parameters.
        if self.peek() == "(" and (not abstract or self.peek(1) in ("*", "(")):
            self.take("(")
            name = self.declarator(abstract=abstract)
            self.take(")")
        elif _IDENTIFIER.fullmatch(self.peek()):
            name = self.take()
        elif not abstract:
            raise Held("m2c", f"header declaration: expected declarator, found {self.peek()!r}")
        while self.peek() in ("[", "("):
            if self.peek() == "[":
                self.take("[")
                self.skip({"]"})
                self.take("]")
            else:
                self.take("(")
                while self.peek() and self.peek() != ")":
                    if self.peek() == "...":
                        self.take()
                    else:
                        while self.peek() in _STORAGE:
                            self.take()
                        self.specifiers()
                        self.declarator(abstract=True)
                    if self.peek() != ",":
                        break
                    self.take(",")
                self.take(")")
        return name

    def declaration(self, *, external: bool = False) -> None:
        storage = set()
        while self.peek() in _STORAGE | _QUALIFIERS:
            storage.add(self.take())
        self.specifiers()
        if self.peek() == ";":
            self.take()
            return
        while True:
            name = "" if self.peek() == ":" else self.declarator()
            if external and "typedef" in storage:
                self.result.typedefs.add(name)
            elif external and "extern" in storage:
                self.result.exports.add(name)
            if self.peek() in (":", "="):
                self.take()
                self.skip({",", ";"})
            if self.peek() != ",":
                break
            self.take(",")
        if external and self.peek() == "{":
            self.take()
            self.skip({"}"})
            self.take("}")
        else:
            self.take(";")

    def parse(self) -> Declarations:
        while self.peek():
            if self.peek() == ";":
                self.take()
            else:
                self.declaration(external=True)
        return self.result


def declarations(source: str) -> Declarations:
    return Parser(source).parse()
