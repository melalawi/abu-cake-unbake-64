"""Partial (NON_MATCHING-guarded) sources and the publication edits of a matched source."""

from __future__ import annotations

import hashlib
import re

from unbake.config import Held
from unbake.process import named as cause_named


def _function(value: object) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z_]\w*", value):
        raise Held(cause_named("function", "function: expected a C identifier", owner="fold.drafts", stage="drafts"))
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
        raise Held(
            cause_named(
                "fold.drafts.unguard", "NON_MATCHING.guard is missing or invalid", owner="fold.drafts", stage="drafts"
            )
        )
    start, end = bounds
    return "".join(line for index, line in enumerate(source.splitlines(keepends=True)) if index not in (start, end))
