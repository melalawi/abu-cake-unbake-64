from __future__ import annotations

import fcntl
import hashlib
import json
import os
import shutil
from collections.abc import Iterable
from contextlib import ExitStack
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
        _workspaces(project)
        _collect(project, generations=generations)


def _workspaces(project: Project) -> None:
    """Remove abandoned command scratch, preserving any live workspace lease."""
    active_setup = False
    patterns = {
        project.build: ("submit-*",),
        project.build / "setup": ("proof-*", "symbol-proof-*"),
    }
    for directory, names in patterns.items():
        if directory.is_symlink():
            continue
        for pattern in names:
            for path in directory.glob(pattern):
                if path.is_symlink() or not path.is_dir():
                    continue
                with (path / ".inuse").open("a+b") as stream:
                    try:
                        fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except BlockingIOError:
                        if directory == project.build / "setup":
                            active_setup = True
                        continue
                    shutil.rmtree(path)
    if not active_setup and not (project.build / "setup").is_symlink():
        _accepted_proposals(project.build / "setup")


def _accepted_proposals(directory: Path) -> None:
    """Drop consumed reviews only when their published evidence agrees."""
    # This historical proposal has no reader; join now owns scoped receipts.
    (directory / "join-proposal.json").unlink(missing_ok=True)
    proposal = directory / "proposal.json"
    confirmation = directory / "confirmation.json"
    if proposal.is_file() and confirmation.is_file():
        try:
            accepted = json.loads(confirmation.read_bytes())["proposal_sha256"]
            with proposal.open("rb") as stream:
                digest = hashlib.file_digest(stream, "sha256").hexdigest()
            if digest == accepted:
                proposal.unlink()
        except (ValueError, KeyError, TypeError):
            pass
    # Replan computes its review anew on confirmation; no command reads this
    # file. Never deserialize historical reviews (hundreds of MB) to collect.
    (directory / "symbol-proposal.json").unlink(missing_ok=True)


def _collect(project: Project, *, generations: Iterable[Path] | None = None) -> None:
    parent = project.build.resolve()
    selected = {path.resolve() for path in generations} if generations is not None else None
    numbered = {
        path.resolve()
        for version in project.versions
        for path in parent.glob(f"{version}.*")
        if path.name.removeprefix(version + ".").isdigit() and path.is_dir() and not path.is_symlink()
    }
    roots = {build.current_generation(project, version).resolve() for version in project.versions}
    roots.update(path for path in numbered if selected is not None and path not in selected)
    # Non-generation workspaces can also retain assembly/assets from a base.
    roots.update(
        path.resolve()
        for path in parent.iterdir()
        if path.is_dir() and not path.is_symlink() and path.resolve() not in numbered
    )
    with ExitStack() as locks:
        for generation in numbered - roots:
            lock = locks.enter_context((generation / ".inuse").open("a+b"))
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                roots.add(generation)
        retained = set(roots)
        pending = list(roots)
        while pending:
            generation = pending.pop()
            obj = generation / "obj"
            paths = (obj,) if obj.is_symlink() else obj.rglob("*") if obj.is_dir() else ()
            for path in paths:
                if not path.is_symlink():
                    continue
                target = path.resolve()
                owner = next((p for p in (target, *target.parents) if p in numbered), None)
                if owner is not None and owner not in retained:
                    retained.add(owner)
                    pending.append(owner)
        for generation in numbered - retained:
            shutil.rmtree(generation)
