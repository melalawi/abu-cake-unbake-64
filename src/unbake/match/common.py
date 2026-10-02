from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path
from typing import NoReturn
from uuid import uuid4

from unbake.project.config import Held, Project

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
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}")
    try:
        temporary.write_bytes(content)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
