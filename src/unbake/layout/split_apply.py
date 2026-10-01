"""Preview and transactionally apply layout edits."""

from __future__ import annotations

import difflib
import os
import shutil
import tempfile
from collections.abc import Iterable
from pathlib import Path
from typing import TYPE_CHECKING

from unbake.layout import split
from unbake.project.config import Held

if TYPE_CHECKING:
    from unbake.project.build import BuildResult
    from unbake.project.config import Policy, Project


def coalesce(edits: Iterable[split.Edit]) -> list[split.Edit]:
    result: dict[Path, split.Edit] = {}
    for edit in edits:
        if not isinstance(edit, split.Edit):
            raise Held("split", "edits: required Edit records")
        path = Path(edit.path)
        if not edit.versions:
            raise Held("split", f"{path}: affected versions")
        if path in result:
            previous = result[path]
            if edit.before != previous.after:
                raise Held("split", f"{path}: conflicting edits")
            result[path] = split.Edit(
                path, previous.before, edit.after, tuple(dict.fromkeys((*previous.versions, *edit.versions)))
            )
        else:
            result[path] = edit
    return [edit for edit in result.values() if edit.before != edit.after]


def diff(edits: Iterable[split.Edit]) -> str:
    return "".join(
        "".join(
            difflib.unified_diff(
                edit.before.splitlines(keepends=True),
                edit.after.splitlines(keepends=True),
                fromfile=str(edit.path),
                tofile=str(edit.path),
            )
        )
        for edit in coalesce(edits)
    )


def write(path: Path, text: str) -> None:
    path = Path(path)
    mode = path.stat().st_mode & 0o777 if path.exists() else 0o644
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=f".{path.name}.", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(text.encode("utf-8"))
            stream.flush()
            os.fsync(stream.fileno())
        temporary.chmod(mode)
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _validated(project: Project, edits: Iterable[split.Edit]) -> tuple[list[split.Edit], list[str]]:
    edits = coalesce(edits)
    if not edits:
        return [], []
    allowed = {
        Path(path).resolve(): v
        for v in project.versions
        for path in (project.version(v).split, project.version(v).symbols)
    }
    affected: set[str] = set()
    for edit in edits:
        path = Path(edit.path)
        shared = False
        source = path.resolve().is_relative_to(Path(project.src).resolve())
        if path.resolve() not in allowed:
            if not hasattr(project, "include"):
                raise Held("split", "project.include: missing value")
            shared = any(path.resolve().is_relative_to(Path(directory).resolve()) for directory in project.include)
        if (path.resolve() not in allowed and not shared and not source) or not path.resolve().is_relative_to(
            project.root.resolve()
        ):
            raise Held("split", f"{path}: edit must target a configured split, symbols, src or include file")
        if (split.read(path) if path.exists() else "") != edit.before:
            raise Held("split", f"{path}: changed since dry run")
        for v in edit.versions:
            project.version(v)
            affected.add(v)
        if shared:
            affected.update(project.versions)
        # Shared configured files affect every VERSION that reads them.
        for v in project.versions:
            version = project.version(v)
            if path.resolve() in (Path(version.split).resolve(), Path(version.symbols).resolve()):
                affected.add(v)
    return edits, [v for v in project.versions if v in affected]


def _write_staging(project: Project, edits: Iterable[split.Edit]) -> None:
    """Write validated edits in a private tree before its full publication proof."""
    edits, _ = _validated(project, edits)
    written: list[tuple[split.Edit, bool]] = []
    try:
        for edit in edits:
            existed = Path(edit.path).exists()
            write(edit.path, edit.after)
            written.append((edit, existed))
    except BaseException:
        for edit, existed in reversed(written):
            if existed:
                write(edit.path, edit.before)
            else:
                Path(edit.path).unlink(missing_ok=True)
        raise


def apply(project: Project, policy: Policy, edits: Iterable[split.Edit]) -> list[BuildResult]:
    from unbake.project import build

    edits, versions = _validated(project, edits)
    if not edits:
        return []
    generations: dict[str, Path] = {}
    links: dict[str, str] = {}
    written: list[tuple[split.Edit, bool]] = []
    published: list[str] = []
    try:
        for v in versions:
            current = build.current_generation(project, v)
            generation_link = project.build_link(v)
            if not generation_link.is_symlink():
                raise Held("split", f"{generation_link}: build generation symlink")
            links[v] = os.readlink(generation_link)
            number = 1
            while True:
                generation = project.build / f"{v}.{number}"
                try:
                    generation.mkdir()
                    break
                except FileExistsError:
                    number += 1
            generations[v] = generation
            shutil.copytree(current, generation, dirs_exist_ok=True, symlinks=True)
        for edit in edits:
            existed = Path(edit.path).exists()
            write(edit.path, edit.after)
            written.append((edit, existed))
        results = build.build(project, policy, versions, tree=project.root, generation_for=generations.__getitem__)
        for v in versions:
            if v not in results:
                raise Held("split", f"VERSION {v}: missing build result")
        ordered = [results[v] for v in versions]
        if any(not result.ok for result in ordered):
            for edit, existed in reversed(written):
                if existed:
                    write(edit.path, edit.before)
                else:
                    Path(edit.path).unlink(missing_ok=True)
            # Keep the failed generations so the returned log paths remain usable.
            return ordered
        for v in versions:
            generation_link = project.build_link(v)
            if not generation_link.is_symlink() or os.readlink(generation_link) != links[v]:
                raise Held("split", f"{generation_link}: generation changed during build")
        for v in versions:
            link(project.build_link(v), generations[v].name)
            published.append(v)
        return ordered
    except BaseException:
        for v in reversed(published):
            link(project.build_link(v), links[v])
        for edit, existed in reversed(written):
            if existed:
                write(edit.path, edit.before)
            else:
                Path(edit.path).unlink(missing_ok=True)
        for generation in generations.values():
            build.discard_generation(generation)
        raise


def link(path: Path, target: str) -> None:
    descriptor, name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    os.close(descriptor)
    temporary = Path(name)
    temporary.unlink()
    try:
        temporary.symlink_to(target)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)
