"""Normalize legacy volatile storage without manufacturing access casts.

Shared externs retain their qualified type in declaration evidence. Scheduling
qualifiers on locals, parameters, fields and ordinary pointer casts can disappear
only after both compilation modes agree in every owning version. Device access
and type queries keep the source rule's existing exceptions.
"""

from __future__ import annotations

import re
import tempfile
from pathlib import Path

from unbake.decomp import checks
from unbake.layout.header_context import Headers
from unbake.config import Held, Host, Project
from unbake.project_tools import atomic as atomic_files

BOUNDARY = "/* unbake declaration evidence boundary */\n"


def externs(source: str, *, conditional: bool = False) -> list[tuple[int, int]]:
    """Locate file-scope extern objects, preserving conditional declaration scope."""
    code = checks._code(source)
    directives = list(re.finditer(r"^[ \t]*#\s*(if|ifdef|ifndef|endif)\b", code, re.M))
    syntax = checks._Syntax(code)
    result = []
    depth = 0
    for index, word in enumerate(syntax.words):
        if word == "extern" and depth == 0:
            level = sum(
                -1 if directive[1] == "endif" else 1
                for directive in directives
                if directive.start() < syntax.tokens[index].start()
            )
            # A conditional inside a function does not condition earlier globals.
            # Conditional externs remain qualified in the source and are held.
            if level and not conditional:
                continue
            end = index + 1
            while end < len(syntax.words) and syntax.words[end] != ";":
                end += 1
            words = syntax.words[index:end]
            if end < len(syntax.words) and "volatile" in words and not set(words) & {"(", "{", "=", "#"}:
                result.append((syntax.tokens[index].start(), syntax.tokens[end].end()))
        depth += {"{": 1, "}": -1}.get(word, 0)
    return result


def candidate(source: str) -> tuple[str, tuple[str, ...]]:
    """Remove only refused qualifier tokens, preserving trusted extern objects."""
    if checks.fakematches(source):
        return source, ()
    prefix, marker, body = source.partition(BOUNDARY)
    if not marker:
        prefix, body = "", source
    code = checks._code(body)
    protected = externs(body, conditional=True)
    refused = checks.volatile_tokens(code)
    patterns = tuple(
        dict.fromkeys(checks.message(checks._finding("volatile-storage", body, token)) for token in refused)
    )
    for token in reversed(refused):
        if not any(start <= token.start() < end for start, end in protected):
            body = body[: token.start()] + " " * len(token[0]) + body[token.end() :]
    return prefix + marker + body, patterns


def proven(
    project: Project,
    policy: Host,
    unit: Path,
    source: str,
    headers: Headers,
    versions: tuple[str, ...],
) -> tuple[str, bool]:
    """Prove removal privately; return source and whether new evidence was added."""
    from unbake.decomp import gbi_proof, work
    from unbake.typemap import declaration_evidence

    if not checks.volatile_tokens(checks._code(source)):
        return source, False
    after, patterns = candidate(source)
    if after != source:
        with tempfile.TemporaryDirectory(prefix="volatile-proof-") as temporary:
            root = Path(temporary)
            staged = work.overlay(project, root)
            for path, text in headers.texts.items():
                destination = root / "overlay" / path.relative_to(project.root)
                destination.parent.mkdir(parents=True, exist_ok=True)
                atomic_files.text(destination, text)
            try:
                gbi_proof.preserve(staged, policy, unit, source, after)
            except Held as error:
                raise Held("volatile", "volatile-storage: " + "; ".join(patterns) + "; " + error.reason) from error
    prefix, marker, body = after.partition(BOUNDARY)
    if not marker:
        prefix, body = "", after
    promoted = []
    for start, end in externs(body):
        statement = body[start:end]
        selected = declaration_evidence.units({unit: statement})
        if len(selected) != 1:
            raise Held("volatile", f"volatile-storage: unsupported shared declaration: {statement}")
        try:
            declaration_evidence.validate_symbols(project, selected, versions)
        except Held as error:
            raise Held("volatile", f"volatile-storage: {statement}; {error.reason}") from error
        names = selected[0].names
        # A shared type already installed is authoritative. A local cast or a
        # new extern must never override its qualifiers or declarator shape.
        expected = checks._Syntax(checks._code(statement)).words
        for existing in declaration_evidence.units(headers.texts):
            if existing.names & names and checks._Syntax(checks._code(existing.text)).words != expected:
                raise Held("volatile", f"volatile-storage: shared declaration conflict: {statement}")
        promoted.append(statement + "\n")
    if not promoted:
        return after, False
    for start, end in reversed(externs(body)):
        body = body[:start] + "".join("\n" if char == "\n" else " " for char in body[start:end]) + body[end:]
    return prefix + "".join(promoted) + BOUNDARY + body, True
