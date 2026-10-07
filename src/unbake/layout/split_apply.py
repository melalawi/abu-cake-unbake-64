"""Preview and transactionally apply layout edits."""

from __future__ import annotations

import difflib
from collections.abc import Iterable
from pathlib import Path
from typing import TYPE_CHECKING

from unbake import atomic as atomic_files
from unbake.config import Held
from unbake.layout import split
from unbake.process import named as cause_named

if TYPE_CHECKING:
    from unbake.build import Outcome
    from unbake.config import Host, Project


def coalesce(edits: Iterable[split.Edit]) -> list[split.Edit]:
    result: dict[Path, split.Edit] = {}
    for edit in edits:
        if not isinstance(edit, split.Edit):
            raise Held(cause_named("edits", "edits: required Edit records", owner="layout.split_apply", stage="split"))
        path = Path(edit.path)
        if not edit.versions:
            raise Held(cause_named(f"{path}", f"{path}: affected versions", owner="layout.split_apply", stage="split"))
        if path in result:
            previous = result[path]
            if edit.before != previous.after:
                raise Held(
                    cause_named(f"{path}", f"{path}: conflicting edits", owner="layout.split_apply", stage="split")
                )
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
        shared = path.resolve() == (project.root / "layout.toml").resolve()
        source = path.resolve().is_relative_to(Path(project.src).resolve())
        if path.resolve() not in allowed and not shared:
            if not hasattr(project, "include"):
                raise Held(
                    cause_named(
                        "project.include", "project.include: missing value", owner="layout.split_apply", stage="split"
                    )
                )
            shared = any(path.resolve().is_relative_to(Path(directory).resolve()) for directory in project.include)
        if (path.resolve() not in allowed and not shared and not source) or not path.resolve().is_relative_to(
            project.root.resolve()
        ):
            raise Held(
                cause_named(
                    f"{path}",
                    f"{path}: edit must target layout.toml or a configured split, symbols, src or include file",
                    owner="layout.split_apply",
                    stage="split",
                )
            )
        if (split.read(path) if path.exists() else "") != edit.before:
            raise Held(
                cause_named(f"{path}", f"{path}: changed since dry run", owner="layout.split_apply", stage="split")
            )
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
    """Write validated edits in their private proved tree through the one Journal."""
    from unbake.journal import Journal

    edits, _ = _validated(project, edits)
    with Journal(project.build / "boundary.journal", root=project.root) as transaction:
        transaction.save(Path(edit.path) for edit in edits)
        for edit in edits:
            atomic_files.text(Path(edit.path), edit.after, encoding="utf-8")


def apply(project: Project, policy: Host, edits: Iterable[split.Edit]) -> Outcome | None:
    """Prepare/prove edits; Journal restores the declared outputs on refusal or death."""
    from unbake import build, buildfiles, steps
    from unbake.journal import Journal

    edits, _ = _validated(project, edits)
    if not edits:
        return None
    prepared = steps.prepare(
        project,
        policy,
        steps.PrepareRequest(
            "boundary",
            (),
            proposed={edit.path: edit.after for edit in edits if edit.path.suffix == ".c"},
            project_scope=True,
        ),
    )
    with Journal(project.build / "boundary.journal", root=project.root) as transaction:
        prepared.assert_current(project)
        transaction.save([*(Path(edit.path) for edit in edits), *buildfiles.generate(project, policy)])
        for edit in edits:
            atomic_files.text(Path(edit.path), edit.after, encoding="utf-8")
        buildfiles.write(project, policy)
        outcome = build.check(project, policy)
        if not outcome.ok:
            transaction.rollback()
    return outcome
