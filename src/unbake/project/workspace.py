"""Command-owned scratch directories with leases visible to explicit collection."""

from __future__ import annotations

import fcntl
import shutil
import tempfile
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from pathlib import Path

from unbake.project import build
from unbake.project.config import PendingProject, Project


@contextmanager
def temporary(project: PendingProject | Project, *, prefix: str, directory: Path) -> Iterator[str]:
    """Publish a leased workspace under the collector lock and always remove it."""
    directory.mkdir(parents=True, exist_ok=True)
    with ExitStack() as cleanup:
        with build._lock(project.build / ".lock"):
            name = cleanup.enter_context(tempfile.TemporaryDirectory(prefix=prefix, dir=directory))
            stream = cleanup.enter_context((Path(name) / ".inuse").open("a+b"))
            fcntl.flock(stream, fcntl.LOCK_SH)
        try:
            yield name
        finally:
            # Remove the directory while its lease is still held.
            try:
                shutil.rmtree(name)
            finally:
                stream.close()
