"""All-or-nothing changes to many project files: a journal of the bytes each path had before.

`with Journal(directory) as journal:` then `journal.save(paths)` before writing them. On any exception the saved
paths get their old bytes back (or are removed when they did not exist). A process killed mid-change leaves the
journal, and `recover(directory)` restores the same way; every Journal recovers before it starts. On success
the journal is discarded.
"""

from __future__ import annotations

import json
import os
import shutil
from collections.abc import Callable, Iterable
from contextlib import AbstractContextManager, nullcontext
from pathlib import Path
from types import TracebackType

from unbake import atomic as atomic_files

INDEX = "index.json"


def recover(directory: Path) -> list[Path]:
    """Restore every path a journal saved, then discard it; returns the restored paths."""
    index = directory / INDEX
    if not index.is_file():
        shutil.rmtree(directory, ignore_errors=True)
        return []
    restored = []
    for row in json.loads(index.read_text()):
        path = Path(row["path"])
        if row["backup"] is None:
            path.unlink(missing_ok=True)
        else:
            atomic_files.write(path, (directory / row["backup"]).read_bytes())
        restored.append(path)
    shutil.rmtree(directory, ignore_errors=True)
    return restored


class Journal:
    def __init__(self, directory: Path, section: Callable[[], AbstractContextManager[object]] = nullcontext) -> None:
        """section: entered around a rollback, so readers never see a half-restored set."""
        self.directory = directory
        self.section = section
        self.rows: list[dict[str, str | None]] = []
        self.saved: set[Path] = set()

    def __enter__(self) -> Journal:
        recover(self.directory)
        self.directory.mkdir(parents=True)
        return self

    def save(self, paths: Iterable[Path]) -> None:
        """Record the current bytes of each path not yet saved; the index is durable before any write."""
        added = False
        for path in sorted(set(paths) - self.saved):
            backup = None
            if path.is_file():
                backup = str(len(self.rows))
                atomic_files.write(self.directory / backup, path.read_bytes())
            self.rows.append({"path": str(path), "backup": backup})
            self.saved.add(path)
            added = True
        if added:
            atomic_files.write(self.directory / INDEX, json.dumps(self.rows).encode())

    def __exit__(
        self, kind: type[BaseException] | None, error: BaseException | None, trace: TracebackType | None
    ) -> None:
        if kind is not None:
            with self.section():
                recover(self.directory)
        else:
            shutil.rmtree(self.directory, ignore_errors=True)


def scratch(parent: Path, prefix: str) -> Path:
    """A new private directory PARENT/PREFIX<pid>-<n>; directories of processes that no longer exist are removed
    first, so a killed process leaks nothing past the next run."""
    parent.mkdir(parents=True, exist_ok=True)
    for stale in parent.glob(prefix + "*"):
        owner = stale.name[len(prefix) :].split("-", 1)[0]
        if owner.isdigit() and not _alive(int(owner)):
            shutil.rmtree(stale, ignore_errors=True)
    for number in range(1 << 16):
        path = parent / f"{prefix}{os.getpid()}-{number}"
        try:
            path.mkdir()
        except FileExistsError:
            continue
        return path
    raise FileExistsError(f"{parent}/{prefix}{os.getpid()}-*: no free name")


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True
