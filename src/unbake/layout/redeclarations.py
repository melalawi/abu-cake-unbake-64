"""Strip equal local typedefs/externs and refuse conflicting imported declarations."""

from __future__ import annotations

import re
from functools import lru_cache

from unbake.decomp.header_declarations import declaration_source, declarations
from unbake.project.config import Held


def spans(text: str) -> list[tuple[int, int]]:
    source = declaration_source(text)
    result = []
    start = 0
    depth = 0
    for token in re.finditer(r'"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'|[{};]', source):
        value = token[0]
        if value == "{":
            depth += 1
        elif value == "}":
            depth -= 1
            if depth == 0 and not re.match(r"\s*(?:typedef|extern)\b", source[start : token.start()]):
                start = token.end()
        elif value == ";" and depth == 0:
            match = re.match(r"\s*(typedef|extern)\b", source[start : token.end()])
            if match:
                result.append((start + match.start(1), token.end()))
            start = token.end()
    return result


def normalized(text: str) -> str:
    return re.sub(r"\s+", "", text)


def variants(text: str) -> tuple[str, ...]:
    """Expand declaration-local conditionals without interpreting project macros."""
    lines = text.splitlines(keepends=True)
    begin = next((i for i, line in enumerate(lines) if re.match(r"\s*#\s*(?:if|ifdef|ifndef)\b", line)), None)
    if begin is None:
        return (text,)
    depth = 0
    branches = []
    start = begin + 1
    for position in range(begin, len(lines)):
        directive = re.match(r"\s*#\s*(if|ifdef|ifndef|elif|else|endif)\b", lines[position])
        if directive is None:
            continue
        kind = directive[1]
        if kind in ("if", "ifdef", "ifndef"):
            depth += 1
        elif kind == "endif":
            depth -= 1
            if not depth:
                branches.append("".join(lines[start:position]))
                prefix, suffix = "".join(lines[:begin]), "".join(lines[position + 1 :])
                return tuple(result for branch in branches for result in variants(prefix + branch + suffix))
        elif depth == 1:
            branches.append("".join(lines[start:position]))
            start = position + 1
    raise Held("layout", "layout.redeclaration: unclosed declaration conditional")


@lru_cache(maxsize=512)
def catalog(header: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for start, end in spans(header):
        declaration = header[start:end]
        for variant in variants(declaration):
            row = declarations(variant)
            for name in row.typedefs | row.declared:
                if name in result and normalized(result[name]) != normalized(variant):
                    raise Held("layout", f"layout.redeclaration.{name}: shared conflict\n{result[name]}\n{variant}")
                result[name] = variant
    return result


def strip(text: str, imported: list[str]) -> str:
    shared: dict[str, str] = {}
    for header in imported:
        for name, declaration in catalog(header).items():
            if name in shared and normalized(shared[name]) != normalized(declaration):
                raise Held("layout", f"layout.redeclaration.{name}: shared conflict\n{shared[name]}\n{declaration}")
            shared[name] = declaration
    for start, end in reversed(spans(text)):
        declaration = text[start:end]
        local = {
            name: variant
            for variant in variants(declaration)
            for name in declarations(variant).typedefs | declarations(variant).declared
        }
        collisions = local.keys() & shared.keys()
        if not collisions:
            continue
        for name in sorted(collisions):
            if normalized(local[name]) != normalized(shared[name]):
                raise Held("layout", f"layout.redeclaration.{name}: local:\n{declaration}\nshared:\n{shared[name]}")
        if collisions != local.keys():
            raise Held("layout", "layout.redeclaration: partially imported conditional declaration\n" + declaration)
        # Remove declaration bytes only; retain preceding comments and directives.
        masked = declaration_source(declaration)
        match = re.search(r"\b(?:typedef|extern)\b", masked)
        assert match is not None
        text = text[: start + match.start()] + text[end:]
    return text
