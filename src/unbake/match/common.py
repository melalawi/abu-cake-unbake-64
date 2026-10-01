from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import shutil
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, NoReturn
from uuid import uuid4

from unbake.decomp import needs
from unbake.layout import split
from unbake.project.config import Held, Project

_FUNCTION = re.compile("[A-Za-z_][A-Za-z_0-9]*\\Z")
QUEUE_PATH = Path(".unbake") / "state" / "match-queue.jsonl"


@dataclass(frozen=True)
class Draft:
    row: dict[str, Any]
    content: bytes
    versions: tuple[str, ...]
    needs: list[needs.Need] = field(default_factory=list)

    @property
    def function(self) -> str:
        return str(self.row["function"])


@dataclass
class Attempt:
    tree: Path
    generations: dict[str, Path]
    failures: list[str]
    edits: list[split.Edit] = field(default_factory=list)
    resolved: list[str] = field(default_factory=list)
    diagnostics: dict[str, str] = field(default_factory=dict)

    def discard(self) -> None:
        shutil.rmtree(self.tree, ignore_errors=True)
        for generation in self.generations.values():
            shutil.rmtree(generation, ignore_errors=True)


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


def queue_path(project: Project) -> Path:
    return project.root / QUEUE_PATH


@contextmanager
def queue_lock(project: Project) -> Iterator[None]:
    path = queue_path(project).with_suffix(".lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def queue(project: Project) -> list[dict[str, Any]]:
    path = queue_path(project)
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for number, line in enumerate(read(path).splitlines(), 1):
        try:
            row = json.loads(line)
        except (ValueError, UnicodeError) as error:
            held(f"{path}:{number}: {error}")
        if not isinstance(row, dict):
            held(f"{path}:{number}: queue row must be an object")
        for key in ("function", "source", "source_sha256"):
            if key not in row:
                held(f"{path}:{number}: missing {key}")
        function(row["function"])
        if not isinstance(row["source"], str) or not Path(row["source"]).is_absolute():
            held(f"{path}:{number}: source must be an absolute path")
        if not isinstance(row["source_sha256"], str) or not re.fullmatch("[0-9a-f]{64}", row["source_sha256"]):
            held(f"{path}:{number}: invalid source_sha256")
        if any(previous["function"] == row["function"] for previous in rows):
            held(f"{path}:{number}: duplicate function {row['function']}")
        rows.append(row)
    return rows


def atomic(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}")
    try:
        temporary.write_bytes(content)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def write_queue(project: Project, rows: list[dict[str, Any]]) -> None:
    atomic(queue_path(project), b"".join((json.dumps(row, sort_keys=True) + "\n").encode() for row in rows))
