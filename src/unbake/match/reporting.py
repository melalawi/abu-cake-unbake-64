"""Durable publication evidence, recorded before expensive follow-up work."""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from pathlib import Path
from time import monotonic
from uuid import uuid4

from unbake.project.config import Project
from unbake.project_tools import atomic as atomic_files

_sink: ContextVar[Callable[[str], object] | None] = ContextVar("publication_sink", default=None)
_report: ContextVar[Path | None] = ContextVar("publication_report", default=None)
_seen: ContextVar[set[str] | None] = ContextVar("publication_seen", default=None)


@contextmanager
def stream(sink: Callable[[str], object]) -> Iterator[None]:
    token = _sink.set(sink)
    try:
        yield
    finally:
        _sink.reset(token)


@contextmanager
def captured() -> Iterator[list[str]]:
    """Collect learned receipts instead of journaling them, for replay in order."""
    lines: list[str] = []
    tokens = _sink.set(lines.append), _report.set(None), _seen.set(None)
    try:
        yield lines
    finally:
        _seen.reset(tokens[2])
        _report.reset(tokens[1])
        _sink.reset(tokens[0])


@contextmanager
def session(project: Project) -> Iterator[None]:
    if _report.get() is not None:
        yield
        return
    path = project.root / ".unbake/state/publications" / (uuid4().hex + ".jsonl")
    path.parent.mkdir(parents=True, exist_ok=True)
    token = _report.set(path)
    seen = _seen.set(set())
    try:
        record("started")
        yield
        record("finished")
    except BaseException:
        record("interrupted")
        raise
    finally:
        _report.reset(token)
        _seen.reset(seen)


def record(event: str, **evidence: object) -> None:
    path = _report.get()
    if path is not None:
        with atomic_files.stream(path, "a") as output:
            output.write(
                json.dumps({"event": event, "at": datetime.now(UTC).isoformat(), **evidence}, sort_keys=True) + "\n"
            )
            output.flush()
            os.fsync(output.fileno())


def learn(line: str) -> None:
    seen = _seen.get()
    if seen is not None and line in seen:
        return
    if seen is not None:
        seen.add(line)
    record("receipt", line=line)
    if sink := _sink.get():
        sink(line)


class Receipts(list[str]):
    def append(self, line: str) -> None:
        super().append(line)
        learn(line)

    def extend(self, lines: Iterable[str]) -> None:
        for line in lines:
            self.append(line)


@contextmanager
def phase(name: str, **evidence: object) -> Iterator[None]:
    """Journal a publication phase with its wall time, even when it stops early."""
    start = monotonic()
    record("phase_started", phase=name, **evidence)
    try:
        yield
    finally:
        seconds = round(monotonic() - start, 3)
        record("phase_finished", phase=name, seconds=seconds, **evidence)
        if sink := _sink.get():
            sink(f"OK(submit): phase {name}: {seconds:.3f} s")
