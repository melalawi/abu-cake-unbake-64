"""Input keys of internal steps, recorded in build/steps.json after each step succeeds."""

from __future__ import annotations

import functools
import json
from pathlib import Path

from unbake.project.cache import key
from unbake.project.config import Held, Project
from unbake.project_tools import atomic as atomic_files


def _path(project: Project) -> Path:
    return project.build / "steps.json"


def recorded(project: Project, step: str) -> str | None:
    path = _path(project)
    if not path.is_file():
        return None
    try:
        value = json.loads(path.read_text())
    except (OSError, ValueError) as error:
        raise Held("steps", f"{path}: {error}") from error
    if not isinstance(value, dict):
        raise Held("steps", f"{path}: expected object")
    result = value.get(step)
    return result if isinstance(result, str) else None


def record(project: Project, step: str, content_key: str) -> None:
    path = _path(project)
    value = json.loads(path.read_text()) if path.is_file() else {}
    value[step] = content_key
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_files.text(path, json.dumps(value, indent=1, sort_keys=True) + "\n")


@functools.cache
def tool_fingerprint() -> str:
    """Digest of the installed tool's code; a tool change reruns every step once."""
    root = Path(__file__).resolve().parent
    return key(*sorted(root.rglob("*.py")))
