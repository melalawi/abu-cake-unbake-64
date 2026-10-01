"""Parse target C declarators and calculate aggregate layouts."""

from __future__ import annotations

import ast
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TypeAlias, cast

from unbake.layout.structs import Field, Layout, held


@dataclass
class _Aggregate:
    kind: str
    name: str
    members: list[_Member] = field(default_factory=list)
    aliases: list[str] = field(default_factory=list)
    start: int = 0
    end: int = 0
    body_start: int = 0
    body_end: int = 0
    complete: bool = False


@dataclass(frozen=True)
class _Member:
    name: str
    base: str | _Aggregate
    operations: tuple[Operation, ...]
    start: int
    end: int


Operation: TypeAlias = tuple[str, int | str | None]

# The target ABI fixes these widths independently of the host running the parser.
_SCALARS = {
    "char": (1, 1),
    "signed char": (1, 1),
    "unsigned char": (1, 1),
    "short": (2, 2),
    "short int": (2, 2),
    "signed short": (2, 2),
    "unsigned short": (2, 2),
    "unsigned short int": (2, 2),
    "int": (4, 4),
    "signed": (4, 4),
    "signed int": (4, 4),
    "unsigned": (4, 4),
    "unsigned int": (4, 4),
    "long": (4, 4),
    "long int": (4, 4),
    "unsigned long": (4, 4),
    "unsigned long int": (4, 4),
    "long long": (8, 8),
    "long long int": (8, 8),
    "unsigned long long": (8, 8),
    "float": (4, 4),
    "double": (8, 8),
    **{f"{sign}{bits}": (bits // 8, bits // 8) for sign in ("s", "u") for bits in (8, 16, 32, 64)},
    "f32": (4, 4),
    "f64": (8, 8),
}
_TOKEN = re.compile(
    r'/\*.*?\*/|//[^\n]*|"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'|0[xX][\da-fA-F]+[uUlL]*|\d+[uUlL]*|[A-Za-z_]\w*|<<|>>|\S',
    re.S,
)
_QUALIFIERS = {
    "const",
    "volatile",
    "restrict",
    "__restrict",
    "signed",
    "unsigned",
    "short",
    "long",
    "int",
    "char",
    "float",
    "double",
    "void",
}


class Parser:
    def __init__(self, source: str) -> None:
        self.source = source
        self.defines = dict(re.findall(r"^\s*#\s*define\s+([A-Za-z_]\w*)[ \t]+([^\n]+)", source, re.M))
        clean = re.sub(r"^\s*#[^\n]*", lambda match: " " * len(match[0]), source, flags=re.M)
        self.tokens = [match for match in _TOKEN.finditer(clean) if not match[0].startswith(("/*", "//"))]
        self.index = 0
        self.types: dict[str, _Aggregate | tuple[str | _Aggregate, tuple[Operation, ...]]] = {}
        self.aggregates: list[_Aggregate] = []
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
        text = re.sub(r"\b0([0-7]+)\b", r"0o\1", text)
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

    def specifier(self) -> str | _Aggregate:
        if self.peek() in ("struct", "union"):
            begin = self.position()
            kind = self.take()
            name = self.take() if re.fullmatch(r"[A-Za-z_]\w*", self.peek()) else ""
            key = f"{kind} {name}"
            aggregate = cast(_Aggregate | None, self.types.get(key)) if name else None
            if aggregate is None:
                aggregate = _Aggregate(kind, name, start=begin)
                if name:
                    self.types[key] = aggregate
            if self.peek() == "{":
                if aggregate.complete:
                    held(key, "duplicate definition")
                self.take("{")
                aggregate.body_start = self.tokens[self.index - 1].end()
                while self.peek() and self.peek() != "}":
                    aggregate.members.extend(self.declaration())
                aggregate.body_end = self.position()
                self.take("}")
                aggregate.end = self.tokens[self.index - 1].end()
                aggregate.complete = True
                self.aggregates.append(aggregate)
            return aggregate
        words = []
        while self.peek() in _QUALIFIERS:
            words.append(self.take())
        if not words or all(word in ("const", "volatile", "restrict", "__restrict") for word in words):
            if not re.fullmatch(r"[A-Za-z_]\w*", self.peek()):
                held("type", f"missing declaration type at {self.position()}")
            words.append(self.take())
        return " ".join(word for word in words if word not in ("const", "volatile", "restrict", "__restrict"))

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
                extent = expression if deferred else self.expression(expression)
                if not deferred and cast(int, extent) <= 0:
                    held(name, f"positive extent required: {expression}")
                operations.append(("array", extent))
            else:
                arguments = self.balanced("(", ")")
                operations.append(("function", re.sub(r"\s+", " ", arguments.strip())))
        return name, operations + pointers

    def declaration(self, typedef: bool = False) -> list[_Member]:
        begin = self.position()
        base = self.specifier()
        result = []
        if self.peek() == ";":
            self.take()
            if isinstance(base, _Aggregate) and base.complete:
                result.append(_Member("", base, (), begin, self.tokens[self.index - 1].end()))
            return result
        while True:
            name, operations = self.declarator(typedef)
            if self.peek() == ":":
                held(name, "bit-field layout requires explicit support")
            if typedef:
                self.types[name] = (base, tuple(operations))
                if isinstance(base, _Aggregate) and not operations:
                    base.aliases.append(name)
                    if not base.name:
                        base.name = name
            else:
                result.append(_Member(name, base, tuple(operations), begin, 0))
            if self.peek() != ",":
                break
            self.take(",")
        self.take(";")
        end = self.tokens[self.index - 1].end()
        if len(result) > 1:
            held(", ".join(item.name for item in result), "split member declarations required")
        return [_Member(item.name, item.base, item.operations, begin, end) for item in result]

    def parse(self) -> list[Layout]:
        while self.peek():
            if self.peek() == "typedef":
                self.take()
                # Non-aggregate typedefs (including compile-time size checks) are
                # retained as type expressions and evaluated only when used.
                self.declaration(typedef=True)
            elif self.peek() in ("struct", "union"):
                saved = self.index
                base = self.specifier()
                if self.peek() == ";":
                    self.take()
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
                if isinstance(base, _Aggregate):
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
        self, base: str | _Aggregate, operations: tuple[Operation, ...], active: tuple[str | int, ...]
    ) -> tuple[int, int, tuple[Field, ...]]:
        # A pointer has a known target width even when its pointee is incomplete.
        children: tuple[Field, ...]
        pointer = next((index for index, op in enumerate(operations) if op[0] == "pointer"), None)
        if pointer is not None:
            size, alignment, children = 4, 4, ()
            operations = operations[:pointer]
        elif isinstance(base, _Aggregate):
            nested = self.layout(base, active)
            size, alignment, children = nested.size, nested.alignment, nested.fields
        elif base in _SCALARS:
            size, alignment = _SCALARS[base]
            children = ()
        elif base in self.types:
            if base in active:
                held(base, "cyclic by-value type")
            target = self.types[base]
            if isinstance(target, _Aggregate):
                return self.type_info(target, operations, (*active, base))
            target_base, target_ops = target
            return self.type_info(target_base, (*operations, *target_ops), (*active, base))
        else:
            held(str(base), "missing type layout")
        for kind, value in reversed(operations):
            if kind == "array":
                extent = self.expression(value) if isinstance(value, str) else cast(int, value)
                if extent <= 0:
                    held(str(base), "positive extent required")
                size *= extent
            else:
                held(str(base), "function member requires a pointer")
        return size, alignment, children

    def type_name(self, base: str | _Aggregate, operations: tuple[Operation, ...], active: tuple[str, ...] = ()) -> str:
        if isinstance(base, str) and base in self.types and base not in active:
            target = self.types[base]
            if isinstance(target, tuple):
                return self.type_name(target[0], (*operations, *target[1]), (*active, base))
        spelling = f"{base.kind} {base.name}".strip() if isinstance(base, _Aggregate) else base
        operations = tuple(
            (kind, self.expression(value) if kind == "array" and isinstance(value, str) else value)
            for kind, value in operations
        )
        return spelling + "".join(
            " *" if kind == "pointer" else f"[{value}]" if kind == "array" else f" ({value})"
            for kind, value in operations
        )

    def layout(self, aggregate: _Aggregate, active: tuple[str | int, ...] = ()) -> Layout:
        key = id(aggregate)
        if key in self.cache:
            return self.cache[key]
        if key in active:
            held(aggregate.name, "cyclic by-value layout")
        if not aggregate.complete:
            held(aggregate.name, "missing aggregate definition")
        fields, size, alignment = [], 0, 1
        for member in aggregate.members:
            width, align, children = self.type_info(member.base, member.operations, (*active, key))
            offset = 0 if aggregate.kind == "union" else (size + align - 1) // align * align
            fields.append(
                Field(
                    member.name,
                    self.type_name(member.base, member.operations),
                    offset,
                    width,
                    tuple(cast(int, value) for kind, value in member.operations if kind == "array"),
                    self.source[member.start : member.end],
                    children,
                    member.start,
                    member.end,
                )
            )
            size = max(size, offset + width)
            alignment = max(alignment, align)
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
