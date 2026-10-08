"""Narrow structural preconditions and function-local source preservation."""

from __future__ import annotations

import re
from typing import Any

from unbake.search.loops import walk


def check_dominance(block: Any, name: str, start: int, end: int, ast: Any) -> bool:
    """Only a straight-line lexical interval proves this local definition/use arm."""
    items = block.block_items or []
    if not 0 <= start < end <= len(items):
        return False
    first = items[start]
    if (
        not isinstance(first, ast.Assignment)
        or first.op != "="
        or not isinstance(first.lvalue, ast.ID)
        or first.lvalue.name != name
    ):
        return False
    if any(isinstance(node, ast.ID) and node.name == name for node in walk(first.rvalue)):
        return False
    hazards = (
        ast.Label,
        ast.Goto,
        ast.If,
        ast.Switch,
        ast.Case,
        ast.Default,
        ast.For,
        ast.While,
        ast.DoWhile,
        ast.Break,
        ast.Continue,
        ast.Return,
        ast.Compound,
    )
    return not any(isinstance(node, hazards) for item in items[start:end] for node in walk(item))


def function_extent(source: str, name: str) -> tuple[int, int]:
    # Mask comments/literals with equal-width whitespace before locating braces.
    masked = re.sub(
        r'/\*.*?\*/|//[^\n]*|"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'',
        lambda m: re.sub(r"[^\n]", " ", m[0]),
        source,
        flags=re.S,
    )
    hits = list(re.finditer(r"\b" + re.escape(name) + r"\s*\([^;{}]*\)\s*\{", masked))
    if len(hits) != 1:
        raise ValueError("mutation.scope: one function definition required")
    brace = masked.index("{", hits[0].start())
    start = max(masked.rfind(";", 0, hits[0].start()), masked.rfind("}", 0, hits[0].start())) + 1
    # Keep preceding include/directive/prototype text outside the replacement.
    directives = list(re.finditer(r"^\s*#[^\n]*\n", masked[start : hits[0].start()], re.M))
    if directives:
        start += directives[-1].end()
    while start < hits[0].start() and source[start].isspace():
        start += 1
    depth = 1
    for index in range(brace + 1, len(masked)):
        depth += (masked[index] == "{") - (masked[index] == "}")
        if depth == 0:
            return start, index + 1
    raise ValueError("mutation.scope: unclosed function body")


def validate_mutation(original: str, changed: str, name: str) -> str:
    """Keep provider declarations, includes and all other functions byte-for-byte."""
    begin, end = function_extent(original, name)
    new_begin, new_end = function_extent(changed, name)
    return original[:begin] + changed[new_begin:new_end] + original[end:]
