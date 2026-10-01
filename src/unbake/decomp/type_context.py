"""Consume the whole-program type solver at the public work boundary."""

from __future__ import annotations

import importlib
from pathlib import Path
from typing import Any

from unbake.decomp.work import digest
from unbake.layout import split
from unbake.project.config import Held, Policy, Project


def provider() -> Any:
    try:
        return importlib.import_module("unbake.typemap")
    except ImportError as error:
        raise Held("types", "types.database: map and solve must provide current type context") from error


def required(project: Project) -> tuple[str, str]:
    api = provider()
    api.load(project, required=True)
    context = api.context(project)
    path = project.build / "types/database.json"
    if not path.is_file():
        raise Held("types", f"types.database: missing {path}")
    return digest(path.read_bytes()), str(context)


def clear_redraft(project: Project, function: str, database: str) -> None:
    provider().clear_redraft(project, function, database)


def redrafts(project: Project) -> dict[str, Any]:
    if not (project.build / "types/redraft.json").exists():
        return {}
    return dict(provider().redrafts(project))


def feedback(
    project: Project,
    function: str,
    source: Path,
    versions: tuple[str, ...],
    targets: dict[str, str],
    *,
    policy: Policy | None = None,
) -> None:
    rom_targets = {}
    for version in versions:
        owners = [row for row in split.functions(project, version) if function in row.aliases]
        if len(owners) != 1:
            raise Held("types", f"types.feedback.target_sha256: {function}: ambiguous owner in {version}")
        rom_targets[version] = digest(split.words(project, owners[0]))
    provider().feedback(
        project,
        function,
        source,
        versions=list(versions),
        policy=policy,
        proof={
            "matched": True,
            "source_sha256": digest(source.read_bytes()),
            "versions": list(versions),
            "target_sha256": rom_targets,
            "target_object_sha256": targets,
        },
    )
