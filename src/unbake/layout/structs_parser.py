"""Parse target C declarators and calculate aggregate layouts."""

from __future__ import annotations

import ast
import re
from collections.abc import Callable
from typing import cast

from unbake.decomp.header_declarations import declaration_source
from unbake.layout.structs import Field, Layout, held
from unbake.layout.structs_types import QUALIFIERS, SCALARS, Aggregate, Declaration, Member, Operation
from unbake.config import Held

_TOKEN = re.compile(
    r'/\*.*?\*/|//[^\n]*|"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'|0[xX][\da-fA-F]+[uUlL]*|\d+[uUlL]*|[A-Za-z_]\w*|<<|>>|\S',
    re.S,
)


class Parser:
    def __init__(self, source: str) -> None:
        self.source = source
        self.defines = dict(re.findall(r"^\s*#\s*define\s+([A-Za-z_]\w*)[ \t]+([^\n]+)", source, re.M))
        clean = declaration_source(source)
        self.tokens = [match for match in _TOKEN.finditer(clean) if not match[0].startswith(("/*", "//"))]
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
        parser = Parser(declaration + ";")
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
        while self.peek():
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
