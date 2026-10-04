from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import NoReturn

from unbake.config import Held, Project
from unbake.project_tools import atomic as atomic_files

_FUNCTION = re.compile("[A-Za-z_][A-Za-z_0-9]*\\Z")


def held(reason: str) -> NoReturn:
    raise Held("match", reason)


def read(path: Path) -> bytes:
    try:
        return path.read_bytes()
    except OSError as error:
        held(f"{path}: {error}")


def sha(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def function(value: object) -> str:
    if not isinstance(value, str) or not _FUNCTION.fullmatch(value):
        held(f"function: invalid or missing {value!r}")
    return value


def relative(project: Project, path: str | Path) -> Path:
    try:
        return Path(path).resolve().relative_to(project.root.resolve())
    except ValueError:
        held(f"{path}: path must be inside project.root {project.root}")


def atomic(path: Path, content: bytes) -> None:
    atomic_files.write(path, content)
