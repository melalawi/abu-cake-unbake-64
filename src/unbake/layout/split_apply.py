"""Preview and transactionally apply layout edits."""

from __future__ import annotations

import difflib
from collections.abc import Iterable
from pathlib import Path
from typing import TYPE_CHECKING

from unbake.layout import split
from unbake.config import Held
from unbake import atomic as atomic_files

if TYPE_CHECKING:
    from unbake.build import Outcome
    from unbake.config import Host, Project


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
    atomic_files.text(Path(path), text, encoding="utf-8")


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


def apply(project: Project, policy: Host, edits: Iterable[split.Edit]) -> Outcome | None:
    """Write the edits, regenerate the build files and prove every version with make check; revert on failure."""
    from unbake import build, buildfiles

    edits, _versions = _validated(project, edits)
    if not edits:
        return None
    written: list[tuple[split.Edit, bool]] = []
    before = {path: path.read_bytes() for path in buildfiles.generate(project, policy) if path.is_file()}

    def revert() -> None:
        for edit, existed in reversed(written):
            if existed:
                write(edit.path, edit.before)
            else:
                Path(edit.path).unlink(missing_ok=True)
        for path, content in before.items():
            atomic_files.write(path, content)

    try:
        for edit in edits:
            existed = Path(edit.path).exists()
            write(edit.path, edit.after)
            written.append((edit, existed))
        buildfiles.write(project, policy)
        outcome = build.check(project, policy)
    except BaseException:
        revert()
        raise
    if not outcome.ok:
        revert()
    return outcome
