"""Notes that fold learns while rewriting a source, collected by the caller (land or tidy)."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

_notes: ContextVar[list[str] | None] = ContextVar("fold_notes", default=None)


def learn(line: str) -> None:
    collected = _notes.get()
    if collected is not None:
        collected.append(line)


@contextmanager
def collect() -> Iterator[list[str]]:
    lines: list[str] = []
    token = _notes.set(lines)
    try:
        yield lines
    finally:
        _notes.reset(token)
