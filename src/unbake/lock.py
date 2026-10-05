"""The one project lock: a kernel flock held by the only writer for its whole life.

The kernel releases it when the process dies, so there is no stale-lock cleanup.
The file records the holder so a refusal can name it.
"""

from __future__ import annotations

import fcntl
import os
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from unbake.config import Held


def path(root: Path) -> Path:
    return Path(root) / "build" / "project.lock"


@contextmanager
def project_lock(root: Path, command: str) -> Iterator[None]:
    target = path(root)
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(target, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            holder = os.pread(descriptor, 4096, 0).decode(errors="replace").strip() or "unknown holder"
            raise Held("lock", f"project.lock: {holder} is writing this project") from None
        started = datetime.now(UTC).isoformat(timespec="seconds")
        record = f"{command} pid {os.getpid()} (started {started})".encode()
        os.ftruncate(descriptor, 0)
        os.pwrite(descriptor, record, 0)
        try:
            yield
        finally:
            os.ftruncate(descriptor, 0)
            fcntl.flock(descriptor, fcntl.LOCK_UN)
    finally:
        os.close(descriptor)


def _flock(target: Path, operation: int) -> int:
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(target, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(descriptor, operation)
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def _release(descriptor: int) -> None:
    fcntl.flock(descriptor, fcntl.LOCK_UN)
    os.close(descriptor)


@contextmanager
def reading(root: Path) -> Iterator[None]:
    """Read include/, the type database and the build files while no step publishes them.

    Readers pass a gate before taking the section shared. A publisher holds the gate while it waits, so new
    readers queue behind it and a steady stream of compares cannot starve a publish."""
    gate = _flock(Path(root) / "build" / "publish.gate", fcntl.LOCK_SH)
    try:
        section = _flock(Path(root) / "build" / "publish.lock", fcntl.LOCK_SH)
    finally:
        _release(gate)
    try:
        yield
    finally:
        _release(section)


_publishing = threading.local()


@contextmanager
def publishing(root: Path) -> Iterator[None]:
    """Replace staged outputs while no reader is inside reading(); readers wait only for this section.

    Reentrant on one thread: a land holds it while the build files it writes take it again."""
    held: dict[Path, int] = _publishing.__dict__.setdefault("held", {})
    key = Path(root)
    if held.get(key):
        held[key] += 1
        try:
            yield
        finally:
            held[key] -= 1
        return
    gate = _flock(key / "build" / "publish.gate", fcntl.LOCK_EX)
    try:
        section = _flock(key / "build" / "publish.lock", fcntl.LOCK_EX)
        try:
            held[key] = 1
            try:
                yield
            finally:
                held[key] = 0
        finally:
            _release(section)
    finally:
        _release(gate)


@contextmanager
def exclusive(target: Path) -> Iterator[None]:
    """A short exclusive section on target, across threads and processes."""
    descriptor = _flock(target, fcntl.LOCK_EX)
    try:
        yield
    finally:
        _release(descriptor)
