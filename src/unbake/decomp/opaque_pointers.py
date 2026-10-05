"""Opaque pointer transport needs no invented pointee layout."""

import re
from typing import Any

from pycparser import c_ast  # type: ignore[import-untyped]

from unbake import cdecl
from unbake.decomp.draft_context import _typedefs
from unbake.decomp.draft_macros import calls

_UNKNOWN = r"M2C_UNK\d*"
_FIELD_TYPE = re.compile(_UNKNOWN + r"\s*\*(\s*\*)+")
_MARK = "\x00unbake-field\x00"
_FIELD_NOTE = "/* types.abi.opaque_pointer: field carries an address only */"
_DECL = re.compile(r"\b(" + _UNKNOWN + r")\s*\*\s*([A-Za-z_]\w*)\s*(?=[,;)=])")


def normalize(source: str, context: str) -> str:
    """Use void pointers only when the source requires no pointee operations."""
    comments = re.compile(r'"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'|/\*.*?\*/|//[^\n]*', re.S)
    clean_context = comments.sub(lambda match: " " if match[0].startswith(("/*", "//")) else match[0], context)
    clean_source = comments.sub(lambda match: " " if match[0].startswith(("/*", "//")) else match[0], source)
    declared = {name for name in re.findall(r"\btypedef\b[^;]*\b(" + _UNKNOWN + r")\s*;", clean_context + clean_source)}
    candidates = {match[2] for match in _DECL.finditer(source) if match[1] not in declared}
    fields: list[bool] = []

    def stand_in(args: list[str]) -> str:
        # Counted in the order calls() visits them, which both passes share.
        pointer = (
            len(args) == 3
            and _FIELD_TYPE.fullmatch(args[1]) is not None
            and args[1].split("*")[0].strip() not in declared
        )
        fields.append(pointer)
        return f"__unbake_field_{len(fields) - 1}" if pointer else "(" + args[0] + ")"

    parsed = calls(source, "M2C_FIELD", stand_in)
    candidates |= {f"__unbake_field_{n}" for n, pointer in enumerate(fields) if pointer}
    if not candidates:
        return source
    # Temporary parser vocabulary never becomes a published declaration.
    names = _typedefs(clean_context) | set(re.findall(r"\b" + _UNKNOWN + r"\b", source))
    parsed = calls(parsed, "M2C_BITWISE", lambda args: "(" + args[-1] + ")")
    parsed = re.sub(r"/\*.*?\*/|//[^\n]*|^\s*#[^\n]*", " ", parsed, flags=re.S | re.M)
    try:
        tree = cdecl.parse("\n".join(f"typedef int {name};" for name in sorted(names)) + "\n" + parsed)
    except cdecl.ParseError:
        return source
    assignments: dict[str, list[Any]] = {name: [] for name in candidates}
    sized: set[str] = set()

    def scan(node: Any) -> None:
        if isinstance(node, c_ast.Assignment) and isinstance(node.lvalue, c_ast.ID) and node.lvalue.name in candidates:
            if node.op == "=":
                assignments[node.lvalue.name].append(node.rvalue)
            elif node.op in ("+=", "-="):
                sized.add(node.lvalue.name)
        if isinstance(node, c_ast.Decl) and node.name in candidates and node.init is not None:
            assignments[node.name].append(node.init)
        if isinstance(node, (c_ast.ArrayRef, c_ast.StructRef)):
            sized.update(name.name for name in ids(node.name) if name.name in candidates)
        if isinstance(node, c_ast.UnaryOp) and node.op in ("*", "sizeof", "p++", "p--", "++", "--"):
            sized.update(name.name for name in ids(node.expr) if name.name in candidates)
        if isinstance(node, c_ast.BinaryOp) and node.op in ("+", "-"):
            sized.update(name.name for name in ids(node) if name.name in candidates)
        for _, child in node.children():
            scan(child)

    def ids(node: Any) -> list[Any]:
        if isinstance(node, c_ast.ID):
            return [node]
        return [item for _, child in node.children() for item in ids(child)]

    scan(tree)
    replacements = {}
    for name in candidates:
        values = assignments[name]
        strings = bool(values) and all(isinstance(value, c_ast.Constant) and value.type == "string" for value in values)
        if strings:
            replacements[name] = ("char", "types.abi.string_pointer: string literals prove char elements")
        elif name not in sized:
            replacements[name] = ("void", "types.abi.opaque_pointer: pointee unknown; only pointer transport required")
    rewritten = _DECL.sub(
        lambda match: (
            replacements[match[2]][0] + " *" + match[2] + " /* " + replacements[match[2]][1] + " */"
            if match[2] in replacements and match[1] not in declared
            else match[0]
        ),
        source,
    )
    transport = {n for n, pointer in enumerate(fields) if pointer and f"__unbake_field_{n}" not in sized}
    if not transport:
        return rewritten
    seen: list[int] = []

    def void_field(args: list[str]) -> str:
        n = len(seen)
        seen.append(n)
        if n in transport:
            return f"M2C_FIELD({args[0]}, {re.sub(_UNKNOWN, 'void', args[1], count=1)}, {args[2]}){_MARK}"
        return f"M2C_FIELD({', '.join(args)})"

    rewritten = calls(rewritten, "M2C_FIELD", void_field)
    while _MARK in rewritten:
        start = rewritten.index(_MARK)
        rewritten = rewritten[:start] + rewritten[start + len(_MARK) :]
        end = rewritten.find(";", start)
        if end < 0:
            raise ValueError("opaque field statement has no terminator")
        rewritten = rewritten[: end + 1] + " " + _FIELD_NOTE + rewritten[end + 1 :]
    return rewritten
