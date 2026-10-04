"""Consume the whole-program type solver at the public work boundary."""

from __future__ import annotations

import importlib
from typing import Any

from unbake.config import Held, Project


def provider() -> Any:
    try:
        return importlib.import_module("unbake.typemap")
    except ImportError as error:
        raise Held("types", "types.database: map and solve must provide current type context") from error


def required(project: Project, function: str | None = None) -> tuple[str, str]:
    from unbake.typemap import database

    context = database.context(project, function=function)
    return database.digest(project), context


def snapshot(project: Project, function: str) -> tuple[str, str]:
    """Read the last solved database without refreshing or publishing headers."""
    return required(project, function)


def clear_redraft(project: Project, function: str, database: str) -> None:
    provider().clear_redraft(project, function, database)


def redrafts(project: Project) -> dict[str, Any]:
    return dict(provider().redrafts(project))
