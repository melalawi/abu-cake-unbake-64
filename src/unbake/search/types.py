"""Integer type spelling mutations: one wrong type is the usual cause of a one-instruction diff."""

import math
import re
import time
from collections.abc import Iterator
from itertools import combinations
from typing import cast

from unbake import cdecl
from unbake.config import Held
from unbake.search.core import Context, Mutation
from unbake.work.compare import Compared

_MASK = re.compile(r'/\*.*?\*/|//[^\n]*|"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'', re.S)
_SPELLING = re.compile(
    r"\b(?:(?:unsigned|signed)\s+)?(?:long\s+long|char|short|int|long)\b|\b[su](?:8|16|32|64)\b"
)
_WIDTHS = {"8": "16", "16": "8 32", "32": "16"}
_WORDS = {"char": "8", "short": "16", "int": "32"}


def _blank(match: re.Match[str]) -> str:
    return re.sub(r"[^\n]", " ", match[0])


def _function_span(masked: str, name: str) -> tuple[int, int] | None:
    """Return the text span from the return type through the closing brace."""
    for found in re.finditer(rf"\b{re.escape(name)}\s*\(", masked):
        depth, index = 1, found.end()
        while index < len(masked) and depth:
            depth += {"(": 1, ")": -1}.get(masked[index], 0)
            index += 1
        rest = re.match(r"\s*\{", masked[index:])
        if not rest:
            continue
        index += rest.end()
        depth = 1
        while index < len(masked) and depth:
            depth += {"{": 1, "}": -1}.get(masked[index], 0)
            index += 1
        start = max(masked.rfind(";", 0, found.start()), masked.rfind("}", 0, found.start())) + 1
        return start, index
    return None


def _singles(spelling: str) -> list[str]:
    """Flip signedness first, then move to a neighbouring width."""
    short = re.fullmatch(r"([su])(8|16|32|64)", spelling)
    if short:
        sign, width = short.groups()
        flipped = [f"{'u' if sign == 's' else 's'}{width}"]
        return flipped + [f"{sign}{wide}" for wide in _WIDTHS.get(width, "").split()]
    prefix = spelling.split()[0] if spelling.split()[0] in ("unsigned", "signed") else ""
    base = spelling.removeprefix(prefix).strip()
    flipped = f"{'signed' if prefix == 'unsigned' else 'unsigned'} {base}"
    result = [flipped]
    width = _WORDS.get(base)
    if width:
        for wide in _WIDTHS[width].split():
            other = next(word for word, bits in _WORDS.items() if bits == wide)
            result.append(f"{prefix} {other}".strip())
    return result


def propose(source: str, trial: Compared, ctx: Context) -> Iterator[Mutation]:
    """Yield unique integer type alternatives for the drafted function, cheapest first."""
    if not isinstance(source, str) or not source.strip():
        raise Held("types", "source is required as C text")
    function_name = getattr(trial, "function", None)
    if not function_name:
        raise Held("types", "trial.function is required")
    if ctx is None:
        raise Held("types", "context is required")
    deadline = getattr(ctx, "deadline", None)
    if type(deadline) not in (int, float) or not math.isfinite(cast(float, deadline)):
        raise Held("types", "context.deadline: finite monotonic time required")
    deadline = cast(float, deadline)
    if time.monotonic() >= deadline:
        return
    try:
        from pycparser import c_ast  # type: ignore[import-untyped]
    except ImportError as error:
        raise Held("types", "pycparser is required") from error
    masked = _MASK.sub(_blank, source)
    if re.search(r"^\s*#", masked, re.M):
        raise Held("types", "source.preprocessed is required (directives remain)")
    try:
        tree = cdecl.parse(masked)
    except (cdecl.ParseError, AssertionError) as error:
        raise Held("types", f"source.syntax: {error}") from error
    if len([node for node in tree.ext if isinstance(node, c_ast.FuncDef) and node.decl.name == function_name]) != 1:
        raise Held("types", f"trial.function {function_name}: exactly one definition required")
    span = _function_span(masked, function_name)
    if span is None:
        raise Held("types", f"trial.function {function_name}: definition text not found")
    start, end = span
    edits = [
        (found.start(), found.end(), found[0], _singles(re.sub(r"\s+", " ", found[0])))
        for found in _SPELLING.finditer(masked, start, end)
    ]
    seen = {source}

    def build(changes: tuple[tuple[int, str], ...]) -> Mutation | None:
        text, parts = source, []
        for index, new in sorted(changes, reverse=True):
            begin, finish, old, _ = edits[index]
            text = text[:begin] + new + text[finish:]
            parts.append((begin, f"{source.count(chr(10), 0, begin) + 1}: {re.sub(chr(32) + '+', ' ', old)} -> {new}"))
        if text in seen:
            return None
        seen.add(text)
        return Mutation("types", ", ".join(line for _, line in sorted(parts)), text)

    singles = [(index, row[3][0]) for index, row in enumerate(edits)] + [
        (index, new) for index, row in enumerate(edits) for new in row[3][1:]
    ]
    for change in singles:
        if time.monotonic() >= deadline:
            return
        mutation = build((change,))
        if mutation:
            yield mutation
    for left, right in combinations(singles, 2):
        if time.monotonic() >= deadline:
            return
        if left[0] == right[0]:
            continue
        mutation = build((left, right))
        if mutation:
            yield mutation
