"""Named input pins and content digests; Cache alone owns cross-operation retention."""

from __future__ import annotations

import hashlib
import os
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Literal

from unbake import cache
from unbake.config import Held

Signature = tuple[int, int, int, int, int]


def signature(path: Path) -> Signature:
    info = path.stat()
    return info.st_dev, info.st_ino, info.st_mtime_ns, info.st_ctime_ns, info.st_size


def bytes_digest(data: bytes, *, algorithm: Literal["sha256", "sha1"]) -> str:
    if algorithm not in ("sha256", "sha1"):
        raise Held("inputs", "inputs.algorithm: expected sha256 or sha1")
    return hashlib.new(algorithm, data).hexdigest()


def digest(path: Path, *, algorithm: Literal["sha256", "sha1"], reuse: bool) -> str:
    """Full stat identity is only a local read optimization; the persisted identity is content."""
    if algorithm not in ("sha256", "sha1"):
        raise Held("inputs", "inputs.algorithm: expected sha256 or sha1")
    path = Path(path)

    def read() -> str:
        before = signature(path)
        with path.open("rb") as stream:
            result = hashlib.file_digest(stream, algorithm).hexdigest()
        if signature(path) != before:
            raise Held("inputs", f"inputs.changed: {path}: changed while reading")
        return result

    if not reuse:
        return read()
    if not cache.configured():
        raise Held("inputs", "cache.memory_bytes: configure before requesting digest reuse")
    return cache.memo(
        "inputs.digest", (str(path.absolute()), signature(path), algorithm), read, size=cache.memory_size, copy_out=str
    )


@dataclass(frozen=True, order=True)
class LogicalPath:
    root: str
    parts: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.root or any(part in ("", ".", "..") or "/" in part or "\\" in part for part in self.parts):
            raise Held("inputs", "inputs.path: named root and safe relative components required")

    @property
    def name(self) -> str:
        return self.root + ":" + "/".join(self.parts)


@dataclass(frozen=True)
class FilePin:
    path: LogicalPath
    state: Literal["file", "missing", "symlink"]
    sha256: str | None
    link_target: LogicalPath | None = None


@dataclass(frozen=True)
class DependencySet:
    files: tuple[FilePin, ...]
    values: Mapping[str, Any]
    recipes: Mapping[str, str]

    @property
    def digest(self) -> str:
        return cache.key(cache.serialized(self.document()))

    def document(self) -> dict[str, Any]:
        return {
            "files": [asdict(pin) for pin in self.files],
            "values": dict(self.values),
            "recipes": dict(self.recipes),
        }


def file_pin(path: Path, *, root: Path, root_id: str, reuse: bool) -> FilePin:
    path, root = Path(path).absolute(), Path(root).absolute()
    if not path.is_relative_to(root):
        raise Held("inputs", "inputs.path: outside named root")
    logical = LogicalPath(root_id, path.relative_to(root).parts)
    target = None
    if path.is_symlink():
        spelling = os.readlink(path)
        candidate = (path.parent / spelling).absolute()
        resolved = candidate.resolve()
        if not resolved.is_relative_to(root.resolve()):
            raise Held("inputs", f"inputs.symlink: {logical.name}: forbidden escape")
        # Keep spelling, including permitted relative traversal, in the pin's digest.
        target = LogicalPath(root_id, resolved.relative_to(root.resolve()).parts)
        sha = (
            cache.key(spelling, digest(path, algorithm="sha256", reuse=reuse))
            if path.is_file()
            else cache.key(spelling, "missing")
        )
        return FilePin(logical, "symlink", sha, target)
    if not path.resolve().is_relative_to(root.resolve()):
        raise Held("inputs", f"inputs.symlink: {logical.name}: forbidden parent escape")
    if not path.is_file():
        return FilePin(logical, "missing", None)
    return FilePin(logical, "file", digest(path, algorithm="sha256", reuse=reuse))


def snapshot(
    view: Any, files: Iterable[LogicalPath], *, values: Mapping[str, Any], recipes: Mapping[str, str]
) -> DependencySet:
    pins = tuple(view.pin(path) for path in sorted(set(files)))
    return DependencySet(pins, dict(values), dict(recipes))


def changed(old: DependencySet, view: Any, *, values: Mapping[str, Any], recipes: Mapping[str, str]) -> tuple[str, ...]:
    differences = [pin.path.name for pin in old.files if pin != view.pin(pin.path)]
    for kind, before, after in (("value", old.values, values), ("recipe", old.recipes, recipes)):
        differences.extend(
            kind + ":" + name for name in set(before) | set(after) if before.get(name) != after.get(name)
        )
    return tuple(sorted(differences))
