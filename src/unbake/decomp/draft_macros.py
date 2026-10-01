"""Lower decompiler placeholders with balanced C argument boundaries."""

import re
from collections.abc import Callable

from unbake.project.config import Held

_TOKEN = re.compile(r'/\*.*?\*/|//[^\n]*|"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'|[A-Za-z_]\w*|\S', re.S)


def calls(source: str, name: str, replace: Callable[[list[str]], str]) -> str:
    """Rewrite nested calls while leaving comments and string literals intact."""
    tokens = list(_TOKEN.finditer(source))
    edits = []
    for index, token in enumerate(tokens):
        if token[0] != name or index + 1 == len(tokens) or tokens[index + 1][0] != "(":
            continue
        depth = 0
        start = tokens[index + 1].end()
        args = []
        for tail in tokens[index + 2 :]:
            word = tail[0]
            if word in ("(", "[", "{"):
                depth += 1
            elif word in (")", "]", "}"):
                if depth == 0:
                    args.append(source[start : tail.start()].strip())
                    edits.append((token.start(), tail.end(), args))
                    break
                depth -= 1
            elif word == "," and depth == 0:
                args.append(source[start : tail.start()].strip())
                start = tail.end()
        else:
            line = source.count("\n", 0, token.start()) + 1
            site = source[token.start() : token.end() + 80]
            raise Held("m2c", f"unresolved {name} at line {line}: {site}")
    # Rewrite the outer call recursively so nested replacements never overlap.
    cursor = 0
    result: list[str] = []
    for start, end, args in edits:
        if start < cursor:
            continue
        result.extend((source[cursor:start], replace([calls(arg, name, replace) for arg in args])))
        cursor = end
    return "".join(result) + source[cursor:]


def lower(source: str, context: str) -> str:
    """Use declared unknown scalar types and preserve lvalue bit reinterpretation."""

    def bitwise(args: list[str]) -> str:
        if len(args) != 2 or not re.fullmatch(r"[A-Za-z_]\w*(?:\s*\*)*", args[0]):
            raise Held("m2c", "unresolved M2C_BITWISE(" + ", ".join(args) + ")")
        target, value = args
        if not re.fullmatch(r"[A-Za-z_]\w*(?:(?:->|\.)[A-Za-z_]\w*|\[[^\]]+\])*", value):
            raise Held("m2c", "unresolved M2C_BITWISE(" + ", ".join(args) + "): requires addressable value")
        return f"(*(({target} *)&({value})))"

    source = calls(source, "M2C_BITWISE", bitwise)
    known = set(re.findall(r"\btypedef\b[^;]*\b(M2C_UNK\d*)\s*;", context + "\n" + source))
    for token in _TOKEN.finditer(source):
        if re.fullmatch(r"M2C_\w+", token[0]) and token[0] not in known:
            line = source.count("\n", 0, token.start()) + 1
            raise Held("m2c", f"unresolved {token[0]} at line {line}: {source.splitlines()[line - 1].strip()}")
    return source
