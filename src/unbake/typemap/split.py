"""Guarded shared declarations with explicit, transitive type prerequisites."""

from __future__ import annotations

import re
from pathlib import Path

from unbake.decomp.header_declarations import declaration_source
from unbake.project.config import Held

_INCLUDE = re.compile(r'^\s*#\s*include\s*[<"]([^>"\n]+)[>"]', re.M)


def required_providers(
    text: str,
    providers: dict[str, set[Path]],
    tags: dict[str, set[Path]],
    aliases: dict[str, str],
    blocked: set[str] | None = None,
    blocked_tags: set[str] | None = None,
) -> set[Path]:
    """Resolve one consumer's names; both generation and imported C use this closure."""
    selected: set[Path] = set()
    code = re.sub(r'"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'', " ", declaration_source(text))
    explicit_tags = set(re.findall(r"\b(?:struct|union|enum)\s+(\w+)", code))
    pending = re.findall(r"\b[A-Za-z_]\w*\b", code)
    seen = set()
    while pending:
        name = pending.pop()
        if name in seen:
            continue
        seen.add(name)
        if name not in (blocked or set()):
            selected.update(providers.get(name, set()))
        if name not in (blocked_tags or set()) and (name not in (blocked or set()) or name in explicit_tags):
            selected.update(tags.get(name, set()))
        if name not in (blocked or set()):
            pending.extend(re.findall(r"\b[A-Za-z_]\w*\b", aliases.get(name, "")))
    return selected


def guarded(path: Path, text: str) -> bytes:
    guard = "UNBAKE_" + re.sub(r"[^A-Za-z0-9]", "_", path.as_posix()).upper()
    return f"#ifndef {guard}\n#define {guard}\n{text}\n#endif\n".encode()


def statements(text: str) -> list[str]:
    """Separate declarations, retaining complete conditional blocks.

    This accepts a header body, without its outer include guard. Directives at
    file scope remain individual units; conditional branches remain together.
    """
    cleaned = declaration_source(text)
    tokens = list(re.finditer(r'"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'|[{};]|\S', cleaned))
    directives = list(re.finditer(r"^[ \t]*#(?:\\\n|[^\n])*", text, re.M))
    events = sorted([(m.start(), "token", m) for m in tokens] + [(m.start(), "directive", m) for m in directives])
    depth = conditional = 0
    start = None
    result = []
    for offset, kind, match in events:
        if start is None:
            start = offset
        if kind == "directive":
            directive = re.match(r"#\s*(\w+)", match[0].lstrip())
            assert directive is not None
            if directive[1] in ("if", "ifdef", "ifndef"):
                conditional += 1
            elif directive[1] == "endif":
                conditional -= 1
                if conditional < 0:
                    raise Held("solve", "types.split: unmatched endif")
            if not conditional and not depth:
                result.append(text[start : match.end()])
                start = None
        else:
            depth += (match[0] == "{") - (match[0] == "}")
            if match[0] == ";" and not depth and not conditional:
                result.append(text[start : match.end()])
                start = None
    if depth or conditional or start is not None:
        raise Held("solve", "types.split: incomplete declaration or conditional")
    return result
