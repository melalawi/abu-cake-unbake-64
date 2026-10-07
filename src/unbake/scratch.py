"""Private tool/proof lifetimes from explicit host configuration, independent of TMPDIR."""

import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from unbake.config import Held, Host, Project
from unbake.process import capture
from unbake.process import named as cause_named


def root(host: Host, project: Project, phase: str) -> Path:
    configured = Path(host.cache_machine_root)
    if not configured.is_absolute():
        raise Held(
            cause_named(
                "cache.machine_root",
                "cache.machine_root: scratch requires an absolute path",
                owner="scratch",
                stage=phase,
            )
        )
    directory = configured.resolve()
    if directory.is_relative_to(project.root.resolve()):
        raise Held(
            cause_named(
                "cache.machine_root",
                "cache.machine_root: scratch requires a directory outside project.root",
                owner="scratch",
                stage=phase,
            )
        )
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        raise Held(
            capture(
                error,
                cause=cause_named(
                    "cache.machine_root",
                    f"cache.machine_root: scratch unavailable: {error}",
                    owner="scratch",
                    stage=phase,
                ),
            )
        ) from error
    return directory


@contextmanager
def temporary(host: Host, project: Project, phase: str, *, prefix: str) -> Iterator[str]:
    directory = root(host, project, phase)
    try:
        pending = tempfile.TemporaryDirectory(prefix=prefix, dir=directory)
    except OSError as error:
        raise Held(
            capture(
                error,
                cause=cause_named(
                    "cache.machine_root",
                    f"cache.machine_root: scratch unavailable: {error}",
                    owner="scratch",
                    stage=phase,
                ),
            )
        ) from error
    with pending as name:
        yield name
