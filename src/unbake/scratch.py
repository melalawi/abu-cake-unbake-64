"""Private tool/proof lifetimes from explicit host configuration, independent of TMPDIR."""

import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from unbake.config import Held, Host, Project


def root(host: Host, project: Project, phase: str) -> Path:
    configured = Path(host.cache_machine_root)
    if not configured.is_absolute():
        raise Held(phase, "cache.machine_root: scratch requires an absolute path")
    directory = configured.resolve()
    if directory.is_relative_to(project.root.resolve()):
        raise Held(phase, "cache.machine_root: scratch requires a directory outside project.root")
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        raise Held(phase, f"cache.machine_root: scratch unavailable: {error}") from error
    return directory


@contextmanager
def temporary(host: Host, project: Project, phase: str, *, prefix: str) -> Iterator[str]:
    directory = root(host, project, phase)
    try:
        pending = tempfile.TemporaryDirectory(prefix=prefix, dir=directory)
    except OSError as error:
        raise Held(phase, f"cache.machine_root: scratch unavailable: {error}") from error
    with pending as name:
        yield name
