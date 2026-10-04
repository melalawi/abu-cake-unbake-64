"""Partial (NON_MATCHING-guarded) sources and the publication edits of a matched source."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable
from pathlib import Path

from unbake.config import Held, Project
from unbake.layout import split


def _function(value: object) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z_]\w*", value):
        raise Held("drafts", "function: expected a C identifier")
    return value


def canonical_source(content: bytes) -> bytes:
    """Remove only the complete publication wrapper; preserve all source bytes."""
    text = content.decode("utf-8")
    return unguard(text).encode("utf-8") if is_partial(text) else content


def source_identity(content: bytes) -> str:
    """Share exact source identity between trials and match submission."""
    return hashlib.sha256(canonical_source(content)).hexdigest()


def _partial_bounds(source: str) -> tuple[int, int] | None:
    """Recognize a complete wrapper after comments/includes, preserving offsets."""
    clean = re.sub(r"/\*.*?\*/|//[^\n]*", lambda match: "\n" * match[0].count("\n"), source, flags=re.S)
    lines = clean.splitlines()
    start = next((index for index, line in enumerate(lines) if line.strip() == "#ifdef NON_MATCHING"), None)
    if start is None or any(line.strip() and not re.fullmatch(r"\s*#\s*include\b.*", line) for line in lines[:start]):
        return None
    depth = 1
    for index, line in enumerate(lines[start + 1 :], start + 1):
        directive = re.match(r"\s*#\s*(if|ifdef|ifndef|endif|else|elif)\b", line)
        if directive:
            kind = directive[1]
            if depth == 1 and kind in ("else", "elif"):
                return None
            depth += 1 if kind in ("if", "ifdef", "ifndef") else -1 if kind == "endif" else 0
            if depth == 0:
                return (
                    (start, index)
                    if line.strip() == "#endif" and not any(tail.strip() for tail in lines[index + 1 :])
                    else None
                )
    return None


def is_partial(source: str) -> bool:
    """Recognize the complete publication wrapper, preserving nested directives."""
    return _partial_bounds(source) is not None


def unguard(source: str) -> str:
    """Remove exactly the outer partial wrapper or refuse its malformed form."""
    bounds = _partial_bounds(source)
    if bounds is None:
        raise Held("drafts", "NON_MATCHING.guard is missing or invalid")
    start, end = bounds
    return "".join(line for index, line in enumerate(source.splitlines(keepends=True)) if index not in (start, end))


def match_edits(project: Project, function: str, source_text: str, versions: Iterable[str]) -> list[split.Edit]:
    """Resolve publication removal and assembly rows into reviewable source/split edits."""
    function = _function(function)
    if not isinstance(source_text, str) or not source_text.strip():
        raise Held("drafts", "source_text is missing")
    if not versions:
        raise Held("drafts", "versions is missing")
    destination = getattr(project, "src", None)
    if not destination:
        raise Held("drafts", "project.src is missing")
    versions = tuple(versions)
    path = Path(destination) / f"{function}.c"
    try:
        before = path.read_text() if path.exists() else ""
        if before and not is_partial(before):
            raise Held("drafts", f"{function}: matched source already exists")
        content = unguard(source_text) if is_partial(source_text) else source_text
        edits = [split.Edit(path, before, content, versions)]
        pattern = re.compile(
            r"^(\s*-\s*\[\s*(?:0[xX][\da-fA-F]+|\d+)\s*,\s*)(asm|c)(\s*,\s*)([^,\]\n]+)([^\n]*\]\s*)$", re.M
        )
        for version in versions:
            split_path = project.version(version).split
            text = split_path.read_text()
            count = 0

            def replace(match: re.Match[str], version: str = version) -> str:
                nonlocal count
                name = match[4].strip().strip("\"'")
                if Path(name).name != function:
                    return match[0]
                count += 1
                if match[2] != "asm":
                    raise Held("drafts", f"{function}: VERSION {version} row requires asm")
                return match[1] + "c" + match[3] + function + match[5]

            after = pattern.sub(replace, text)
            if count != 1:
                raise Held("drafts", f"{function}: VERSION {version} requires one asm row, found {count}")
            edits.append(split.Edit(split_path, text, after, (version,)))
        return edits
    except (OSError, UnicodeError) as error:
        raise Held("drafts", f"{function}: {error}") from error
