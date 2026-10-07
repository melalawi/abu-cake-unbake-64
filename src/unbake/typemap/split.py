"""Guarded shared declarations with explicit, transitive type prerequisites."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

from unbake import cache as retention
from unbake.cache import memo
from unbake.cdecl import SOURCE_TOKEN, NameParser, declaration_context, declaration_source
from unbake.config import Held

_INCLUDE = re.compile(r'^\s*#\s*include\s*[<"]([^>"\n]+)[>"]', re.M)


def required_providers(
    text: str,
    providers: dict[str, set[Path]],
    tags: dict[str, set[Path]],
    aliases: dict[str, str],
    blocked: set[str] | None = None,
    blocked_tags: set[str] | None = None,
    *,
    preferred: set[Path] | None = None,
) -> set[Path]:
    """Resolve one consumer's names; both generation and imported C use this closure."""
    from unbake.layout.apply import spelled

    selected: set[Path] = set()

    def offered(paths: set[Path]) -> set[Path]:
        # Existing include scope proves which declaration view this consumer
        # uses. Without that evidence retain every alternative for the normal
        # contradiction checks; never choose a type by path or address usage.
        return (paths & preferred) or paths if preferred is not None else paths

    code = re.sub(r'"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'', " ", declaration_source(text))
    explicit_tags = set(re.findall(r"\b(?:struct|union|enum)\s+(\w+)", code))
    pending = list(spelled(text))
    seen = set()
    while pending:
        name = pending.pop()
        if name in seen:
            continue
        seen.add(name)
        if name not in (blocked or set()):
            selected.update(offered(providers.get(name, set())))
        if name not in (blocked_tags or set()) and (name not in (blocked or set()) or name in explicit_tags):
            selected.update(offered(tags.get(name, set())))
        if name not in (blocked or set()):
            pending.extend(re.findall(r"\b[A-Za-z_]\w*\b", aliases.get(name, "")))
    return selected


def guarded(path: Path, text: str) -> bytes:
    guard = "UNBAKE_" + re.sub(r"[^A-Za-z0-9]", "_", path.as_posix()).upper()
    return f"#ifndef {guard}\n#define {guard}\n{text}\n#endif\n".encode()


def statements(text: str) -> tuple[str, ...]:
    """Separate declarations, retaining complete conditional blocks.

    This accepts a header body, without its outer include guard. Directives at
    file scope remain individual units; conditional branches remain together.
    The result is shared by every caller of equal text, so it is a tuple.
    """
    return memo(
        "typemap-statements",
        hashlib.sha256(text.encode()).digest(),
        lambda: _split(text),
        size=retention.memory_size,
        copy_out=retention.clone,
    )


def _function_body(prefix: str, decorations: frozenset[str]) -> bool:
    """Let the declaration parser distinguish a body from an aggregate/initializer."""
    try:
        row = NameParser(prefix + " {}", decorations=decorations).parse()
    except Held:
        return False
    return bool(row.declared) and not row.typedefs


def _split(text: str) -> tuple[str, ...]:
    _, decorations = declaration_context(text)
    tokens = SOURCE_TOKEN.finditer(text)
    directives = re.finditer(r"^[ \t]*#(?:\\\n|[^\n])*", text, re.M)
    events = sorted([(m.start(), "token", m) for m in tokens] + [(m.start(), "directive", m) for m in directives])
    pairs = {"{": "}", "(": ")", "[": "]"}
    stack: list[str] = []
    conditional = consumed = 0
    start = declaration = None
    function = False
    previous = ""
    result = []
    for offset, kind, match in events:
        if offset < consumed:
            continue
        token = match[0]
        if kind == "token" and token.startswith(("/*", "//")):
            # SOURCE_TOKEN keeps comments and quoted braces atomic. A continued
            # line comment also owns any directive-looking following lines.
            comment = re.match(r"//(?:\\\n|[^\n])*", text[offset:]) if token.startswith("//") else None
            consumed = offset + comment.end() if comment else match.end()
            continue
        if start is None:
            start = offset
        if kind == "directive":
            consumed = match.end()
            directive = re.match(r"#\s*(\w+)", token.lstrip())
            if directive is None:
                continue
            if directive[1] in ("if", "ifdef", "ifndef"):
                conditional += 1
            elif directive[1] == "endif":
                conditional -= 1
                if conditional < 0:
                    raise Held("solve", "types.split: unmatched endif")
            elif directive[1] in ("else", "elif") and not conditional:
                raise Held("solve", "types.split: unmatched conditional branch")
            if not conditional and not stack and declaration is None:
                result.append(text[start : match.end()])
                start = None
            continue
        if declaration is None:
            declaration = offset
        boundary = False
        if token in pairs:
            if token == "{" and not stack:
                function = previous == ")" and _function_body(text[declaration:offset], decorations)
            stack.append(pairs[token])
        elif token in pairs.values():
            if not stack or stack.pop() != token:
                raise Held("solve", "types.split: unmatched declaration delimiter")
            boundary = token == "}" and not stack and function
        elif token == ";" and not stack:
            boundary = True
        if boundary:
            declaration = None
            function = False
            if not conditional:
                result.append(text[start : match.end()])
                start = None
        previous = token
    if stack or conditional or start is not None:
        raise Held("solve", "types.split: incomplete declaration or conditional")
    return tuple(result)
