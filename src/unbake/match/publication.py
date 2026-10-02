from __future__ import annotations

import fcntl
import os
import shutil
from collections.abc import Iterable
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


def collect(project: Project, *, generations: Iterable[Path] | None = None) -> None:
    """Serialize discovery with reader pinning; never wait for active generations."""
    with build.lock(project):
        _collect(project, generations=generations)


def _collect(project: Project, *, generations: Iterable[Path] | None = None) -> None:
    parent = project.build
    selected = set(generations) if generations is not None else None
    retained: set[Path] = set()
    for generation in parent.iterdir():
        if not generation.is_dir() or generation.is_symlink():
            continue
        for name in ("asm", "assets"):
            path = generation / "obj" / name
            if path.is_symlink():
                target = path.resolve()
                retained.update(p for p in target.parents if p.parent == parent)
    for version in project.versions:
        live = build.current_generation(project, version).resolve()
        for generation in parent.glob(f"{version}.*"):
            suffix = generation.name.removeprefix(version + ".")
            if (
                (selected is not None and generation not in selected)
                or not suffix.isdigit()
                or not generation.is_dir()
                or generation.is_symlink()
                or (generation.resolve() == live)
                or generation.resolve() in retained
            ):
                continue
            with (generation / ".inuse").open("a+b") as lock:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    continue
                shutil.rmtree(generation)
