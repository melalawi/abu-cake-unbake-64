"""The one place C declaration text is parsed.

- declaration_source / attribute_source blank comments, directives and attributes without moving offsets.
- NameParser reads declarators without needing typedefs declared first: declarations(text) gives the names a
  text declares and uses (memoised, kind `decl.names`).
- LayoutParser reads aggregate definitions and computes target layouts: records(text) (memoised, kind
  `decl.records`).
- parse(text, typedefs=...) returns a fresh pycparser AST; `typedefs` seeds names a preprocessed prefix
  already declared, so a unit can be parsed without re-reading its headers.
"""

from __future__ import annotations

import ast
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any, cast

from pycparser import c_ast, c_parser  # type: ignore[import-untyped]

from unbake.config import Held
from unbake.layout.structs import Field, Layout, held
from unbake.layout.structs_types import QUALIFIERS, SCALARS, Aggregate, Declaration, Member, Operation

ParseError = c_parser.ParseError
NAME_TOKEN = re.compile(r'"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'|[A-Za-z_]\w*|\.\.\.|\S')
SOURCE_TOKEN = re.compile(r'/\*.*?\*/|//[^\n]*|"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'|[A-Za-z_]\w*|\S', re.S)


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
    if "__attribute" not in source:
        return source
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


class NameParser:
    """Parse declaration specifiers and recursive (including abstract) declarators.

    A typedef name is an identifier in a type position. Its definition is not
    needed to discover dependencies; names in parameters, fields and extents
    belong to different positions and cannot become type providers or uses.
    """

    def __init__(self, source: str) -> None:
        source = re.sub(r"\\\n", "", attribute_source(declaration_source(source)))
        self.tokens = NAME_TOKEN.findall(source)
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
    from unbake.cache import memo

    # A version fold reads the same installed declarations through several
    # private include trees. Text, rather than their temporary paths, identifies
    # this analysis. Keep caller-owned sets outside the shared cache.
    parsed = memo("decl.names", source, lambda: NameParser(source).parse(), keep=32768)
    return Declarations(
        set(parsed.typedefs),
        set(parsed.uses),
        set(parsed.exports),
        set(parsed.tags),
        set(parsed.complete_uses),
        set(parsed.complete_alias_uses),
        set(parsed.declared),
    )


LAYOUT_TOKEN = re.compile(
    r'/\*.*?\*/|//[^\n]*|"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'|0[xX][\da-fA-F]+[uUlL]*|\d+[uUlL]*|[A-Za-z_]\w*|<<|>>|\S',
    re.S,
)


def layout_tokens(clean: str, start: int = 0, end: int | None = None) -> list[re.Match[str]]:
    """LayoutParser's tokens of declaration_source text between start and end."""
    matches = LAYOUT_TOKEN.finditer(clean, start, len(clean) if end is None else end)
    return [match for match in matches if not match[0].startswith(("/*", "//"))]


class LayoutParser:
    def __init__(self, source: str, tokens: list[re.Match[str]] | None = None) -> None:
        """tokens: the layout tokens of declaration_source(source), when the caller already has them."""
        self.source = source
        self.defines = dict(re.findall(r"^\s*#\s*define\s+([A-Za-z_]\w*)[ \t]+([^\n]+)", source, re.M))
        self.tokens = layout_tokens(declaration_source(source)) if tokens is None else tokens
        self.index = 0
        self.types: dict[str, Aggregate | tuple[str | Aggregate, tuple[Operation, ...]]] = {}
        self.aggregates: list[Aggregate] = []
        self.declarations: list[Declaration] = []
        self.cache: dict[int, Layout] = {}

    def peek(self) -> str:
        return self.tokens[self.index][0] if self.index < len(self.tokens) else ""

    def take(self, expected: str | None = None) -> str:
        token = self.peek()
        if not token or (expected is not None and token != expected):
            held(expected or "declaration", f"expected token, found {token!r}")
        self.index += 1
        return token

    def position(self) -> int:
        return self.tokens[self.index].start() if self.index < len(self.tokens) else len(self.source)

    def expression(self, text: str, active: tuple[str, ...] = ()) -> int:
        text = re.sub(r"\b(0[xX][\da-fA-F]+|\d+)[uUlL]+\b", r"\1", text)
        text = re.sub(r"/\*.*?\*/|//[^\n]*", "", text, flags=re.S)
        text = re.sub(r"\bsizeof\s*\(([^()]*)\)", lambda match: str(self.sizeof_type(match[1])), text)
        text = re.sub(r"\b0([0-7]+)\b", r"0o\1", text).strip()
        try:
            node = ast.parse(text, mode="eval").body
        except SyntaxError:
            held(text, "integer extent required")

        def evaluate(value: ast.expr) -> int:
            if isinstance(value, ast.Constant) and type(value.value) is int:
                return value.value
            if isinstance(value, ast.Name):
                if value.id not in self.defines:
                    held(value.id, "missing integer extent")
                if value.id in active:
                    held(value.id, "cyclic integer extent")
                return self.expression(self.defines[value.id], (*active, value.id))
            if isinstance(value, ast.UnaryOp):
                operand = evaluate(value.operand)
                if isinstance(value.op, ast.USub):
                    return -operand
                if isinstance(value.op, ast.UAdd):
                    return operand
                if isinstance(value.op, ast.Invert):
                    return ~operand
            if isinstance(value, ast.BinOp):
                left, right = evaluate(value.left), evaluate(value.right)
                operations: dict[type[ast.operator], Callable[[], int]] = {
                    ast.Add: lambda: left + right,
                    ast.Sub: lambda: left - right,
                    ast.Mult: lambda: left * right,
                    ast.Div: lambda: (abs(left) // abs(right)) * (-1 if (left < 0) != (right < 0) else 1),
                    ast.Mod: lambda: (
                        left - ((abs(left) // abs(right)) * (-1 if (left < 0) != (right < 0) else 1)) * right
                    ),
                    ast.LShift: lambda: left << right,
                    ast.RShift: lambda: left >> right,
                    ast.BitOr: lambda: left | right,
                    ast.BitAnd: lambda: left & right,
                    ast.BitXor: lambda: left ^ right,
                }
                if type(value.op) in operations:
                    try:
                        return operations[type(value.op)]()
                    except (ZeroDivisionError, ValueError):
                        held(text, "invalid integer extent")
            held(text, "unsupported integer extent")

        return evaluate(node)

    def sizeof_type(self, spelling: str) -> int:
        """Resolve sizeof type operands using the same target declarator rules."""
        declaration = spelling.replace("[", " __extent[", 1) if "[" in spelling else spelling + " __extent"
        parser = LayoutParser(declaration + ";")
        parser.types = self.types
        parser.defines = self.defines
        parser.cache = self.cache
        member = parser.declaration()[0]
        return self.type_info(member.base, member.operations, ())[0]

    def balanced(self, opening: str, closing: str) -> str:
        self.take(opening)
        begin = self.position()
        depth = 1
        while self.peek():
            token = self.take()
            depth += (token == opening) - (token == closing)
            if depth == 0:
                return self.source[begin : self.tokens[self.index - 1].start()]
        held(opening, f"missing {closing}")

    def specifier(self) -> str | Aggregate:
        while self.peek() in ("const", "volatile", "restrict", "__restrict"):
            self.take()
        if self.peek() in ("struct", "union"):
            begin = self.position()
            kind = self.take()
            name = self.take() if re.fullmatch(r"[A-Za-z_]\w*", self.peek()) else ""
            key = f"{kind} {name}"
            aggregate = cast(Aggregate | None, self.types.get(key)) if name else None
            if aggregate is None:
                aggregate = Aggregate(kind, name, start=begin)
                if name:
                    self.types[key] = aggregate
            if self.peek() == "{":
                if aggregate.complete:
                    held(key, "duplicate definition")
                aggregate.start = begin
                self.take("{")
                aggregate.body_start = self.tokens[self.index - 1].end()
                while self.peek() and self.peek() != "}":
                    aggregate.members.extend(self.declaration())
                aggregate.body_end = self.position()
                self.take("}")
                aggregate.end = self.tokens[self.index - 1].end()
                aggregate.complete = True
                self.aggregates.append(aggregate)
            while self.peek() in ("const", "volatile", "restrict", "__restrict"):
                self.take()
            return aggregate
        words = []
        while self.peek() in QUALIFIERS:
            words.append(self.take())
        if not words or all(word in ("const", "volatile", "restrict", "__restrict") for word in words):
            if not re.fullmatch(r"[A-Za-z_]\w*", self.peek()):
                held("type", f"missing declaration type at {self.position()}")
            words.append(self.take())
        while self.peek() in ("const", "volatile", "restrict", "__restrict"):
            self.take()
        words = [word for word in words if word not in ("const", "volatile", "restrict", "__restrict")]
        if set(words) <= {"signed", "unsigned", "short", "long", "int", "char"}:
            sign = "unsigned " if "unsigned" in words else "signed " if "char" in words and "signed" in words else ""
            width = "char" if "char" in words else "short" if "short" in words else "long " * words.count("long")
            return sign + (width.strip() or "int")
        return " ".join(words)

    def declarator(self, deferred: bool = False) -> tuple[str, list[Operation]]:
        pointers: list[Operation] = []
        while self.peek() == "*":
            self.take()
            while self.peek() in ("const", "volatile", "restrict", "__restrict"):
                self.take()
            pointers.append(("pointer", None))
        if self.peek() == "(":
            self.take()
            name, operations = self.declarator(deferred)
            self.take(")")
        else:
            name, operations = self.take(), []
            if not re.fullmatch(r"[A-Za-z_]\w*", name):
                held(name, "member name required")
        while self.peek() in ("[", "("):
            if self.peek() == "[":
                expression = self.balanced("[", "]")
                extent = expression if deferred else self.expression(expression) if expression.strip() else 0
                if not deferred and cast(int, extent) < 0:
                    held(name, f"positive extent required: {expression}")
                operations.append(("array", extent))
            else:
                arguments = self.balanced("(", ")")
                operations.append(("function", re.sub(r"\s+", " ", arguments.strip())))
        return name, operations + pointers

    def declaration(self, typedef: bool = False) -> list[Member]:
        begin = self.position()
        try:
            return self.members(typedef)
        except Held as error:
            line = self.source.count("\n", 0, begin) + 1
            held(error.reason, f"line {line}")

    def members(self, typedef: bool = False) -> list[Member]:
        begin = self.position()
        base = self.specifier()
        specifier = self.source[begin : self.position()].strip()
        result = []
        if self.peek() == ";":
            self.take()
            if isinstance(base, Aggregate) and base.complete:
                result.append(Member("", base, (), begin, self.tokens[self.index - 1].end()))
            return result
        while True:
            start = self.position()
            name, operations = ("", []) if self.peek() == ":" else self.declarator(typedef)
            bits = None
            if self.peek() == ":":
                self.take(":")
                extent_start = self.position()
                depth = 0
                while self.peek() and (depth or self.peek() not in (",", ";")):
                    token = self.take()
                    depth += (token == "(") - (token == ")")
                bits = self.expression(self.source[extent_start : self.position()])
                if typedef or operations or bits < 0 or (name and bits == 0):
                    held(name or "bit-field", "invalid bit-field declarator")
            declaration = specifier + " " + self.source[start : self.position()].strip() + ";"
            if typedef:
                self.types[name] = (base, tuple(operations))
                if isinstance(base, Aggregate) and not operations:
                    base.aliases.append(name)
                    if not base.name:
                        base.name = name
            result.append(Member(name, base, tuple(operations), begin, 0, declaration, bits))
            if self.peek() != ",":
                break
            self.take(",")
        if self.peek() != ";":
            held(name or "member", f"unparsed declarator token {self.peek()!r}")
        self.take(";")
        end = self.tokens[self.index - 1].end()
        return [
            Member(item.name, item.base, item.operations, begin, end, item.declaration, item.bits) for item in result
        ]

    def parse(self) -> list[Layout]:
        self.declare(len(self.tokens))
        return self.layouts()

    def declare(self, stop: int) -> None:
        """Read top-level declarations until the token index reaches stop (or the tokens end)."""
        while self.index < stop and self.peek():
            begin = self.position()
            if self.peek() == "typedef":
                self.take()
                # Non-aggregate typedefs (including compile-time size checks) are
                # retained as type expressions and evaluated only when used.
                members = self.declaration(typedef=True)
                end = self.tokens[self.index - 1].end()
                self.declarations.extend(Declaration(item.base, item.operations, begin, end) for item in members)
            elif self.peek() in ("struct", "union"):
                saved = self.index
                base = self.specifier()
                if self.peek() == ";":
                    self.take()
                    self.declarations.append(Declaration(base, (), begin, self.tokens[self.index - 1].end()))
                else:
                    self.index = saved
                    self.skip_external()
            else:
                self.skip_external()

    def layouts(self) -> list[Layout]:
        """Give each aggregate its typedef chain aliases, then lay out every named aggregate."""
        for name, target in self.types.items():
            if isinstance(target, tuple) and not target[1]:
                base = target[0]
                seen = {name}
                while isinstance(base, str) and base in self.types and base not in seen:
                    seen.add(base)
                    next_type = self.types[base]
                    if not isinstance(next_type, tuple) or next_type[1]:
                        break
                    base = next_type[0]
                if isinstance(base, Aggregate):
                    base.aliases.append(name)
        return [self.layout(item) for item in self.aggregates if item.name]

    def skip_external(self) -> None:
        while self.peek():
            token = self.take()
            if token == ";":
                return
            if token == "{":
                self.index -= 1
                self.balanced("{", "}")
                return
            if token == "(":
                self.index -= 1
                self.balanced("(", ")")

    def type_info(
        self, base: str | Aggregate, operations: tuple[Operation, ...], active: tuple[str | int, ...]
    ) -> tuple[int, int, tuple[Field, ...]]:
        # A pointer has a known target width even when its pointee is incomplete.
        children: tuple[Field, ...]
        pointer = next((index for index, op in enumerate(operations) if op[0] == "pointer"), None)
        if pointer is not None:
            size, alignment, children = 4, 4, ()
            operations = operations[:pointer]
        elif isinstance(base, Aggregate):
            nested = self.layout(base, active)
            size, alignment, children = nested.size, nested.alignment, nested.fields
        elif base in SCALARS:
            size, alignment = SCALARS[base]
            children = ()
        elif base in self.types:
            if base in active:
                held(base, "cyclic by-value type")
            target = self.types[base]
            if isinstance(target, Aggregate):
                return self.type_info(target, operations, (*active, base))
            target_base, target_ops = target
            return self.type_info(target_base, (*operations, *target_ops), (*active, base))
        else:
            held(str(base), "missing type layout")
        for kind, value in reversed(operations):
            if kind == "array":
                extent = self.expression(value) if isinstance(value, str) else cast(int, value)
                if extent < 0:
                    held(str(base), "positive extent required")
                size *= extent
            else:
                held(str(base), "function member requires a pointer")
        return size, alignment, children

    def type_name(self, base: str | Aggregate, operations: tuple[Operation, ...], active: tuple[str, ...] = ()) -> str:
        if isinstance(base, str) and base in self.types and base not in active:
            target = self.types[base]
            if isinstance(target, tuple):
                return self.type_name(target[0], (*operations, *target[1]), (*active, base))
        spelling = f"{base.kind} {base.name}".strip() if isinstance(base, Aggregate) else base
        operations = tuple(
            (kind, self.expression(value) if kind == "array" and isinstance(value, str) else value)
            for kind, value in operations
        )
        return spelling + "".join(
            " *" if kind == "pointer" else f"[{value}]" if kind == "array" else f" ({value})"
            for kind, value in operations
        )

    def layout(self, aggregate: Aggregate, active: tuple[str | int, ...] = ()) -> Layout:
        key = id(aggregate)
        if key in self.cache:
            return self.cache[key]
        if key in active:
            held(aggregate.name, "cyclic by-value layout")
        if not aggregate.complete:
            held(aggregate.name, "missing aggregate definition")
        fields, size, alignment = [], 0, 1
        bit_cursor = 0
        for member in aggregate.members:
            try:
                width, align, children = self.type_info(member.base, member.operations, (*active, key))
            except Held as error:
                line = self.source.count("\n", 0, member.start) + 1
                held(f"{aggregate.name}.{member.name or '<anonymous>'}", f"{error.reason}; line {line}")
            bit_offset = None
            if member.bits is not None:
                spelling = self.type_name(member.base, ())
                if spelling not in SCALARS or spelling in ("float", "double", "f32", "f64") or member.bits > width * 8:
                    line = self.source.count("\n", 0, member.start) + 1
                    held(member.name or aggregate.name, f"invalid bit-field type or width; line {line}")
                if aggregate.kind == "union":
                    bit_cursor = 0
                elif member.bits == 0 or bit_cursor % (align * 8) + member.bits > width * 8:
                    bit_cursor = (bit_cursor + align * 8 - 1) // (align * 8) * (align * 8)
                offset, bit_offset = divmod(bit_cursor, 8)
                bit_cursor += member.bits
                size = max(size, (bit_cursor + 7) // 8)
                if member.name:
                    alignment = max(alignment, align)
                width = (bit_offset + member.bits + 7) // 8
            else:
                offset = 0 if aggregate.kind == "union" else (size + align - 1) // align * align
                bit_cursor = (offset + width) * 8
                size = max(size, offset + width)
                alignment = max(alignment, align)
            fields.append(
                Field(
                    member.name,
                    self.type_name(member.base, member.operations),
                    offset,
                    width,
                    tuple(cast(int, value) for kind, value in member.operations if kind == "array"),
                    member.declaration or self.source[member.start : member.end],
                    children,
                    member.start,
                    member.end,
                    bit_offset,
                    member.bits,
                )
            )
        size = (size + alignment - 1) // alignment * alignment
        result = Layout(
            aggregate.name,
            aggregate.kind,
            tuple(fields),
            size,
            alignment,
            tuple(dict.fromkeys(aggregate.aliases)),
            self.source,
            aggregate.start,
            aggregate.end,
            aggregate.body_start,
            aggregate.body_end,
        )
        self.cache[key] = result
        return result


class SeededParser(c_parser.CParser):  # type: ignore[misc]
    """Resume the file scope of an exact preprocessed prefix (pinned pycparser 3)."""

    def __init__(self, scope: dict[str, bool]) -> None:
        super().__init__()
        self.scope = scope

    def _parse_translation_unit_or_empty(self) -> Any:
        self._scope_stack = [self.scope.copy()]
        return super()._parse_translation_unit_or_empty()


def located(text: str, message: str) -> str:
    """A parse error "file:line:col: reason" against a combined unit, restated against the real file through the
    unit's cpp linemarkers (`# 12 "src/a.c"`), with the offending line. Cleaning keeps line numbers."""
    found = re.match(r"^([^:]*):(\d+):(\d+): (.*)$", message, re.S)
    if found is None:
        return message
    number, column, reason = int(found[2]), found[3], found[4]
    lines = text.splitlines()
    if not 1 <= number <= len(lines):
        return message
    where = f"unit line {number}"
    for index in range(number - 2, -1, -1):
        marker = re.match(r'^\s*#\s*(\d+)\s+"([^"]+)"', lines[index])
        if marker:
            where = f"{marker[2]}:{int(marker[1]) + number - index - 2}"
            break
    return f"{where}:{column}: {reason}: {lines[number - 1].strip()}"


_MARKER = re.compile(r'^[ \t]*#[ \t]*(?:line[ \t]+)?(\d+)(?:[ \t]+"((?:\\.|[^"\\])*)")?[^\n]*$', re.M)


def resume_marker(text: str, start: int) -> str:
    """A line marker that gives text[start:] the coordinates it has inside TEXT: the file of the last marker before
    START (none when there is none) and START's line. A resumed parse keeps every node where a whole parse puts it."""
    found = next(reversed(list(_MARKER.finditer(text, 0, start))), None)
    if found is None:
        return f"# {text.count(chr(10), 0, start) + 1}\n"
    line = int(found[1]) + text.count("\n", found.end() + 1, start)
    return f'# {line} "{found[2]}"\n' if found[2] is not None else f"# {line}\n"


def resumable_parse(text: str, scope: dict[str, bool]) -> c_ast.FileAST:
    """parser(scope).parse(text), resumed after the longest checkpointed prefix shared with a recent unit.

    pycparser's only state between top-level declarations is the file scope, so a unit split at a
    checkpoint parses as the prefix's nodes followed by the rest seeded with the prefix's scope.
    A unit that does not parse is parsed whole, so its error is the same as without resumption.
    """
    from unbake import prefixes

    State = tuple[tuple[Any, ...], dict[str, bool]]

    def advance(state: State | None, whole: str, start: int, end: int) -> State | None:
        nodes, before = state or ((), scope)
        head = parser(before)
        try:
            tree = head.parse(resume_marker(whole, start) + whole[start:end])
        except Exception:
            return None
        return (*nodes, *tree.ext), head._scope_stack[0].copy()

    def finish(state: State | None, whole: str, start: int) -> c_ast.FileAST:
        if state is None:
            return parser(scope).parse(whole)
        nodes, after = state
        try:
            rest = parser(after).parse(resume_marker(whole, start) + whole[start:])
        except Exception:
            return parser(scope).parse(whole)
        return c_ast.FileAST([*nodes, *rest.ext])

    return prefixes.resumed("cdecl.parse." + repr(sorted(scope.items())), text, advance, finish)


class GnuParser(c_parser.CParser):  # type: ignore[misc]
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


def parser(typedefs: Iterable[str] | dict[str, bool] = ()) -> c_parser.CParser:
    scope = typedefs if isinstance(typedefs, dict) else dict.fromkeys(typedefs, True)
    return SeededParser(scope) if scope else c_parser.CParser()


def parse(text: str, *, typedefs: Iterable[str] | dict[str, bool] = ()) -> c_ast.FileAST:
    return parser(typedefs).parse(text)


def records(text: str) -> list[Any]:
    from unbake.cache import memo

    return list(memo("decl.records", text, lambda: LayoutParser(text).parse(), keep=32768))
