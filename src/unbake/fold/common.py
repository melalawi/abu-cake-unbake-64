from __future__ import annotations

import re
from pathlib import Path
from typing import NoReturn

from unbake.config import Held, Project
from unbake.process import named as cause_named

_FUNCTION = re.compile("[A-Za-z_][A-Za-z_0-9]*\\Z")


def held(reason: str) -> NoReturn:
    raise Held(cause_named("fold.common.held", reason, owner="fold.common", stage="match"))


def read(path: Path) -> bytes:
    try:
        return path.read_bytes()
    except OSError as error:
        held(f"{path}: {error}")


def function(value: object) -> str:
    if not isinstance(value, str) or not _FUNCTION.fullmatch(value):
        held(f"function: invalid or missing {value!r}")
    return value


def relative(project: Project, path: str | Path) -> Path:
    try:
        return Path(path).resolve().relative_to(project.root.resolve())
    except ValueError:
        held(f"{path}: path must be inside project.root {project.root}")
