"""Restore rejected publication writes through the existing project journal."""

from __future__ import annotations

import os
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from functools import wraps
from pathlib import Path
from typing import Any, TypeVar

from unbake import atomic
from unbake.config import Project
from unbake.journal import Journal


@dataclass(frozen=True)
class Entry:
    mode: int | None
    link: str | None
    times: tuple[int, int] | None


class Publication:
    def __init__(self, project: Project):
        self.project = project
        self.directory = project.build / "publication.journal"
        self.journal = Journal(self.directory)
        self.entries: dict[Path, Entry] = {}
        self.saving = False

    def owns(self, path: Path) -> bool:
        # Do not resolve the final component: replacing a symlink changes that
        # directory entry, not the external file to which it points.
        path = Path(os.path.abspath(path))
        if not path.is_relative_to(self.project.root):
            return False
        relative = path.relative_to(self.project.root)
        if ".git" in relative.parts or any(part.endswith(".journal") for part in relative.parts):
            return False
        return not any(
            path.is_relative_to(root)
            for root in (self.project.cache, self.project.work / "_land", self.project.work / "_headers")
        )

    def save(self, path: Path) -> None:
        path = Path(os.path.abspath(path))
        if self.saving or not self.owns(path) or path in self.entries:
            return
        link = str(path.readlink()) if path.is_symlink() else None
        mode = path.stat().st_mode & 0o777 if path.is_file() and link is None else None
        stat = path.lstat() if path.exists() or path.is_symlink() else None
        times = (stat.st_atime_ns, stat.st_mtime_ns) if stat is not None else None
        self.entries[path] = Entry(mode, link, times)
        self.saving = True
        try:
            self.journal.save((path,))
        finally:
            self.saving = False

    def accepted(self) -> None:
        """A Git commit is durable even if a following callback or step stops."""
        if not self.entries:
            return
        self.saving = True
        try:
            self.journal.__exit__(None, None, None)
            self.journal = Journal(self.directory).__enter__()
            self.entries.clear()
        finally:
            self.saving = False

    def finish(self, error: BaseException | None) -> None:
        self.saving = True
        try:
            self.journal.__exit__(
                type(error) if error is not None else None, error, error.__traceback__ if error else None
            )
            if error is not None:
                for path, entry in self.entries.items():
                    if entry.link is not None:
                        path.unlink(missing_ok=True)
                        path.symlink_to(entry.link)
                    elif entry.mode is not None and path.is_file():
                        path.chmod(entry.mode)
                    if entry.times is not None and (path.exists() or path.is_symlink()):
                        os.utime(path, ns=entry.times, follow_symlinks=False)
        finally:
            self.saving = False


_current: ContextVar[Publication | None] = ContextVar("publication.transaction", default=None)


def accepted() -> None:
    operation = _current.get()
    if operation is not None:
        operation.accepted()


@contextmanager
def transaction(project: Project) -> Iterator[Publication]:
    existing = _current.get()
    if existing is not None:
        if existing.project.root != project.root:
            raise RuntimeError("publication transaction cannot change project roots")
        yield existing
        return
    operation = Publication(project)
    operation.journal.__enter__()
    token = _current.set(operation)
    error: BaseException | None = None
    try:
        with atomic.recording(operation.save):
            yield operation
    except BaseException as caught:
        error = caught
        raise
    finally:
        _current.reset(token)
        operation.finish(error)


F = TypeVar("F", bound=Callable[..., Any])


def transactional(function: F) -> F:
    @wraps(function)
    def guarded(project: Project, *args: Any, **kwargs: Any) -> Any:
        with transaction(project):
            return function(project, *args, **kwargs)

    from typing import cast

    return cast(F, guarded)
