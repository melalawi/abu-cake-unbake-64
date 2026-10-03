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


def attribute_source(source: str) -> str:
    """Hide balanced GCC attribute clauses while preserving all source offsets."""
    tokens = list(re.finditer(r'"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'|[A-Za-z_]\w*|\S', source))
    edits = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        index += 1
        if token[0] not in ("__attribute__", "__attribute"):
            continue
        if index >= len(tokens) or tokens[index][0] != "(":
            raise Held("m2c", "header declaration: expected ( after attribute")
        depth = 0
        while index < len(tokens):
            closing = tokens[index]
            depth += (closing[0] == "(") - (closing[0] == ")")
            index += 1
            if not depth:
                edits.append((token.start(), closing.end()))
                break
        else:
            raise Held("m2c", "header declaration: unclosed attribute")
    for start, end in reversed(edits):
        source = source[:start] + re.sub(r"[^\n]", " ", source[start:end]) + source[end:]
    return source


@dataclass
class Declarations:
    typedefs: set[str] = field(default_factory=set)
    uses: set[str] = field(default_factory=set)
    exports: set[str] = field(default_factory=set)
    tags: set[str] = field(default_factory=set)
    complete_uses: set[str] = field(default_factory=set)
    complete_alias_uses: set[str] = field(default_factory=set)
    declared: set[str] = field(default_factory=set)


class Parser:
    """Parse declaration specifiers and recursive (including abstract) declarators.

    A typedef name is an identifier in a type position. Its definition is not
    needed to discover dependencies; names in parameters, fields and extents
    belong to different positions and cannot become type providers or uses.
    """

    def __init__(self, source: str) -> None:
        source = re.sub(r"\\\n", "", attribute_source(declaration_source(source)))
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

    def specifiers(self) -> tuple[str, str]:
        referenced_tag = ""
        referenced_alias = ""
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
            referenced_alias = name
        while self.peek() in _QUALIFIERS:
            self.take()

        return referenced_tag, referenced_alias

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
                        tag, alias = self.specifiers()
                        _, indirect = self.declarator(abstract=True)
                        if tag and not indirect:
                            self.result.complete_uses.add(tag)
                        if alias and not indirect:
                            self.result.complete_alias_uses.add(alias)
                    if self.peek() != ",":
                        break
                    self.take(",")
                self.take(")")
        return name, pointer

    def declaration(self, *, external: bool = False) -> None:
        storage = set()
        while self.peek() in _STORAGE | _QUALIFIERS:
            storage.add(self.take())
        tag, alias = self.specifiers()
        if self.peek() == ";":
            self.take()
            return
        while True:
            name, indirect = ("", False) if self.peek() == ":" else self.declarator()
            if tag and not indirect and "typedef" not in storage:
                self.result.complete_uses.add(tag)
            if alias and not indirect and "typedef" not in storage:
                self.result.complete_alias_uses.add(alias)
            if external and "typedef" in storage:
                self.result.typedefs.add(name)
            elif external and "extern" in storage:
                self.result.exports.add(name)
            if external and "typedef" not in storage and name:
                self.result.declared.add(name)
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
    from unbake.project.cache import remembered

    # A version fold reads the same installed declarations through several
    # private include trees. Text, rather than their temporary paths, identifies
    # this analysis. Keep caller-owned sets outside the shared cache.
    parsed = remembered("headers.declarations", source, lambda: Parser(source).parse(), keep=2048)
    return Declarations(
        set(parsed.typedefs),
        set(parsed.uses),
        set(parsed.exports),
        set(parsed.tags),
        set(parsed.complete_uses),
        set(parsed.complete_alias_uses),
        set(parsed.declared),
    )
