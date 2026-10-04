"""Consume the whole-program type solver at the public work boundary."""

from __future__ import annotations

import importlib
from typing import Any

from unbake.decomp.work import digest
from unbake.config import Held, Project


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
    return required(project, function)


def clear_redraft(project: Project, function: str, database: str) -> None:
    provider().clear_redraft(project, function, database)


def redrafts(project: Project) -> dict[str, Any]:
    if not (project.build / "types/redraft.json").exists():
        return {}
    return dict(provider().redrafts(project))
