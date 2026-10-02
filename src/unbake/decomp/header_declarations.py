"""Read header declarators without requiring their typedefs to be ordered first."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from unbake.project.config import Held

_IDENTIFIER = re.compile(r"[A-Za-z_]\w*\Z")
_SCALARS = set(["void", "char", "short", "int", "long", "float", "double", "signed", "unsigned", "_Bool", "_Complex"])
_QUALIFIERS = set(["const", "volatile", "restrict", "__restrict", "__restrict__"])
_STORAGE = set(["typedef", "extern", "static", "auto", "register", "inline", "__inline", "__inline__", "__extension__"])


def declaration_source(source: str) -> str:
    """Hide comments and complete logical directives without moving edit offsets."""

    def blank(match: re.Match[str]) -> str:
        return "".join("\n" if char == "\n" else " " for char in match[0])

    source = re.sub(
        r'"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'|/\*.*?\*/|//(?:\\\n|[^\n])*',
        lambda match: blank(match) if match[0].startswith(("/*", "//")) else match[0],
        source,
        flags=re.S,
    )
    return re.sub(r"^[ \t]*#(?:\\\n|[^\n])*", blank, source, flags=re.M)


@dataclass
class Declarations:
    typedefs: set[str] = field(default_factory=set)
    uses: set[str] = field(default_factory=set)
    exports: set[str] = field(default_factory=set)
    tags: set[str] = field(default_factory=set)
    complete_uses: set[str] = field(default_factory=set)


class Parser:
    """Parse declaration specifiers and recursive (including abstract) declarators.

    A typedef name is an identifier in a type position. Its definition is not
    needed to discover dependencies; names in parameters, fields and extents
    belong to different positions and cannot become type providers or uses.
    """

    def __init__(self, source: str) -> None:
        source = re.sub(r"\\\n", "", declaration_source(source))
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

    def specifiers(self) -> str:
        referenced_tag = ""
        while self.peek() in _QUALIFIERS:
            self.take()
        if self.peek() in ("struct", "union", "enum"):
            kind = self.take()
            tag = self.take() if _IDENTIFIER.fullmatch(self.peek()) else ""
            if self.peek() == "{":
                if tag:
                    self.result.exports.add(tag)
                    if kind != "enum":
                        self.result.tags.add(tag)
                self.take("{")
                if kind == "enum":
                    self.skip({"}"})
                else:
                    while self.peek() and self.peek() != "}":
                        self.declaration()
                self.take("}")
            elif kind != "enum":
                referenced_tag = tag
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

        return referenced_tag

    def declarator(self, *, abstract: bool = False) -> tuple[str, bool]:
        pointer = False
        while self.peek() == "*":
            pointer = True
            self.take()
            while self.peek() in _QUALIFIERS:
                self.take()
        name = ""
        # Parentheses group a declarator; suffix parentheses contain parameters.
        if self.peek() == "(" and (not abstract or self.peek(1) in ("*", "(")):
            self.take("(")
            name, nested_pointer = self.declarator(abstract=abstract)
            pointer |= nested_pointer
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
                        tag = self.specifiers()
                        _, indirect = self.declarator(abstract=True)
                        if tag and not indirect:
                            self.result.complete_uses.add(tag)
                    if self.peek() != ",":
                        break
                    self.take(",")
                self.take(")")
        return name, pointer

    def declaration(self, *, external: bool = False) -> None:
        storage = set()
        while self.peek() in _STORAGE | _QUALIFIERS:
            storage.add(self.take())
        tag = self.specifiers()
        if self.peek() == ";":
            self.take()
            return
        while True:
            name, indirect = ("", False) if self.peek() == ":" else self.declarator()
            if tag and not indirect and "typedef" not in storage:
                self.result.complete_uses.add(tag)
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
