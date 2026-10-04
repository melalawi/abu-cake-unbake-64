"""Validate rewritten C and headers with the ordinary cartridge build."""

from __future__ import annotations

import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from unbake.project.config import Held, Policy, Project
from unbake.project_tools import atomic as atomic_files


def publish(project: Project, policy: Policy | None, outputs: dict[Path, bytes | Path]) -> int:
    from unbake.layout import apply
    from unbake.project import build

    count = apply.install(project, outputs)
    if policy is None:
        from unbake.project.config import read_policy

        policy = read_policy()
    results = build.build(
        project,
        policy,
        project.versions,
        tree=project.root,
        generation_for=lambda version: build.current_generation(project, version),
    )
    if any(not result.ok for result in results.values()):
        raise Held("layout", "layout.proof: rewritten project failed cartridge check")
    return count


@contextmanager
def transaction(project: Project) -> Iterator[None]:
    """Keep the source/header/type publication reversible across solve and build."""
    paths = {p for root in (*project.include, project.src) for p in root.rglob("*") if p.is_file()}
    paths.update(p for folder in ("types", "layout", "map") for p in (project.build / folder).glob("*.json"))
    paths.update(project.version(version).split for version in project.versions)
    with tempfile.TemporaryDirectory(prefix=".layout-backup-", dir=project.build) as directory:
        backups = {}
        for number, path in enumerate(sorted(paths)):
            backup = Path(directory) / str(number)
            atomic_files.copy2(path, backup)
            backups[path] = backup
        try:
            yield
        except BaseException:
            after = {p for root in (*project.include, project.src) for p in root.rglob("*") if p.is_file()}
            after.update(p for folder in ("types", "layout", "map") for p in (project.build / folder).glob("*.json"))
            for path in after - paths:
                path.unlink()
            for path, backup in backups.items():
                path.parent.mkdir(parents=True, exist_ok=True)
                atomic_files.copy2(backup, path)
            raise
