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


def required(project: Project, function: str | None = None) -> tuple[str, str]:
    api = provider()
    api.load(project, required=True)
    context = api.context(project, function=function) if function is not None else api.context(project)
    path = project.build / "types/database.json"
    if not path.is_file():
        raise Held("types", f"types.database: missing {path}")
    return digest(path.read_bytes()), str(context)


def snapshot(project: Project, function: str) -> tuple[str, str]:
    """Read the last solved database without refreshing or publishing headers."""
    api = provider()
    try:
        return required(project, function)
    except Held as error:
        if not error.reason.startswith("types.inputs_stale:"):
            raise
        api.load(project, allow_stale=True)
        print("type database snapshot: stale; " + error.reason)
        path = project.build / "types/database.json"
        return digest(path.read_bytes()), str(api.context(project, function=function, allow_stale=True))


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


def feedback_many(
    project: Project,
    entries: list[tuple[str, Path, tuple[str, ...], dict[str, str]]],
    *,
    policy: Policy | None = None,
) -> None:
    proofs = []
    for function, source, versions, targets in entries:
        rom_targets = {}
        for version in versions:
            owners = [row for row in split.functions(project, version) if function in row.aliases]
            if len(owners) != 1:
                raise Held("types", f"types.feedback.target_sha256: {function}: ambiguous owner in {version}")
            rom_targets[version] = digest(split.words(project, owners[0]))
        proofs.append(
            {
                "function": function,
                "source": source,
                "versions": list(versions),
                "proof": {
                    "matched": True,
                    "source_sha256": digest(source.read_bytes()),
                    "versions": list(versions),
                    "target_sha256": rom_targets,
                    "target_object_sha256": targets,
                },
            }
        )
    api = provider()
    if not hasattr(api, "feedback_many"):
        raise Held("types", "types.feedback.batch: whole-program provider lacks feedback_many")
    api.feedback_many(project, proofs, policy=policy)
