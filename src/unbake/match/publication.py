from __future__ import annotations

import fcntl
import os
import shutil
from pathlib import Path
from uuid import uuid4

from unbake.project import build
from unbake.project.config import Project


def swap(link: Path, target: Path) -> None:
    temporary = link.with_name(f".{link.name}.{uuid4().hex}")
    try:
        temporary.symlink_to(target.name)
        os.replace(temporary, link)
    finally:
        temporary.unlink(missing_ok=True)


def collect(project: Project) -> None:
    """Serialize discovery with reader pinning; never wait for active generations."""
    with build.lock(project):
        _collect(project)


def _collect(project: Project) -> None:
    parent = project.build
    for version in project.versions:
        live = build.current_generation(project, version).resolve()
        for generation in parent.glob(f"{version}.*"):
            suffix = generation.name.removeprefix(version + ".")
            if (
                not suffix.isdigit()
                or not generation.is_dir()
                or generation.is_symlink()
                or (generation.resolve() == live)
            ):
                continue
            with (generation / ".inuse").open("a+b") as lock:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    continue
                shutil.rmtree(generation)
