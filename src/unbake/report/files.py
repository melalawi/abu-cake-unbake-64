"""Atomic writes for generated reports and project documents."""

from pathlib import Path

from unbake.project_tools import atomic as atomic_files


def write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_files.write(path, content)
