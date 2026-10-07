"""The one project lock: a kernel flock held by the only writer for its whole life.

The kernel releases it when the process dies, so there is no stale-lock cleanup.
The file records the holder so a refusal can name it.
"""

from __future__ import annotations

import fcntl
import os
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from unbake.config import Held
from unbake.process import named as cause_named


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
            raise Held(
                cause_named(
                    "project.lock", f"project.lock: {holder} is writing this project", owner="lock", stage="lock"
                )
            ) from None
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
