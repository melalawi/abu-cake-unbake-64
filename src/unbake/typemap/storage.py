"""Pinned project evidence and atomic generated records."""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import tempfile
from functools import lru_cache
from pathlib import Path
from typing import Any

from unbake.project.config import Held, Project


def digest(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _stamp(path: Path) -> tuple[int, int, int, int, int]:
    stat = path.stat()
    return stat.st_dev, stat.st_ino, stat.st_mtime_ns, stat.st_ctime_ns, stat.st_size


_file_digests: dict[Path, tuple[tuple[int, int, int, int, int], str]] = {}


def file_digest(path: Path) -> str:
    stamp = _stamp(path)
    cached = _file_digests.get(path)
    if cached is not None and cached[0] == stamp:
        return cached[1]
    hash_ = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            hash_.update(block)
    result = hash_.hexdigest()
    _file_digests[path] = stamp, result
    return result


def encoded(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise Held("solve", f"types.database: generated path is a symlink: {path}")
    descriptor, temporary = tempfile.mkstemp(prefix=".typemap-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def read(path: Path, key: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_bytes())
    except (OSError, ValueError) as error:
        raise Held("solve", f"{key}: {path}: {error}") from error
    if not isinstance(value, dict) or value.get("schema") != 1:
        raise Held("solve", f"{key}: schema=1 object required")
    return value


def identity(project: Project) -> dict[str, Any]:
    return {
        "schema": 1,
        "project_id": project.id,
        "workspace_id": project.workspace_id,
        "rom_sha1": {v: project.version(v).baserom_sha1 for v in project.versions},
    }


def validate_identity(project: Project, value: dict[str, Any], key: str) -> None:
    if any(value.get(field) != expected for field, expected in identity(project).items()):
        raise Held("solve", f"{key}: project/workspace/ROM identity changed")


def changed_source(project: Project) -> Path | None:
    """Find the first published source that needs a new cartridge proof."""
    proven = project.build / "types/proven.json"
    if not proven.is_file():
        return None
    value = read(proven, "types.feedback")
    validate_identity(project, value, "types.feedback")
    for row in value.get("records", {}).values():
        source: Path = project.root / row["source"]
        if not source.is_file() or file_digest(source) != row["source_sha256"]:
            return source
    return None


def submit_command(project: Project, source: Path) -> str:
    return shlex.join(["unbake", "--project", str(project.root), "submit", str(source)])


def inputs(project: Project, *, headers: bool = False) -> dict[str, str]:
    paths = {project.root / "config.toml"}
    for version in project.versions:
        configured = project.version(version)
        if not configured.baserom.is_file():
            raise Held("map", f"map.rom_sha1.{version}: missing {configured.baserom}")
        paths.add(configured.baserom)
        for filename in ("splat_symbols.csv", "symbol-addresses.txt"):
            generated_symbols = project.build_link(version) / filename
            if generated_symbols.is_file():
                paths.add(generated_symbols)
        for key, path in (("split", configured.split), ("symbols", configured.symbols)):
            if not path.is_file():
                raise Held("map", f"map.{key}.{version}: missing {path}")
            paths.add(path)
    paths.update(path for version in project.versions for path in (project.asm / version).rglob("*.s"))
    paths.update(path for path in (project.build / "setup/layout.json",) if path.is_file())
    if headers:
        paths.update(path for root in project.include for path in root.rglob("*.h") if not generated(project, path))
        proven = project.build / "types/proven.json"
        if proven.is_file():
            paths.add(proven)
            stale = changed_source(project)
            if stale is not None:
                raise Held(
                    "solve",
                    f"types.feedback.source_sha256: published source changed: {stale}; "
                    f"re-prove with {submit_command(project, stale)}",
                )
            for row in read(proven, "types.feedback").get("records", {}).values():
                source = project.root / row["source"]
                paths.add(source)
    return {str(path.relative_to(project.root)): file_digest(path) for path in sorted(paths)}


@lru_cache(maxsize=32)
def _generated_paths(root: Path) -> tuple[frozenset[str], tuple[str, ...]]:
    shared = root / "shared"
    directories = tuple(str(shared / name) for name in ("types", "decls", "consumers"))
    exact = frozenset((str(shared / "typemap.h"), str(shared / "prototypes.h"), *directories))
    return exact, tuple(name + os.sep for name in directories)


def generated(project: Project, path: Path) -> bool:
    if not project.include:
        return False
    exact, prefixes = _generated_paths(project.include[0])
    name = str(path)
    return name in exact or name.startswith(prefixes)


def relative(project: Project, path: Path) -> str:
    """Use already normalized project paths without scanning pathlib ancestors."""
    return relative_root(project.root, path)


def relative_root(root_path: Path, path: Path) -> str:
    name, root = str(path), str(root_path)
    if name == root:
        return "."
    prefix = root + os.sep
    if name.startswith(prefix):
        return name[len(prefix) :]
    return str(path.relative_to(root_path))


class FactLog:
    """Stream redundant diagnostic constraints instead of retaining them in RAM."""

    def __init__(self, directory: Path) -> None:
        directory.mkdir(parents=True, exist_ok=True)
        descriptor, name = tempfile.mkstemp(prefix=".constraints-", dir=directory)
        self.temporary = Path(name)
        self.stream = os.fdopen(descriptor, "w")
        self.count = 0

    def append(self, value: dict[str, Any]) -> None:
        self.stream.write(json.dumps(value, separators=(",", ":")) + "\n")
        self.count += 1

    def finish(self, root: Path) -> dict[str, Any]:
        self.stream.close()
        digest_ = file_digest(self.temporary)
        path = self.temporary.parent / ("constraints-" + digest_ + ".jsonl")
        os.replace(self.temporary, path)
        return {"kind": "shard", "path": str(path.relative_to(root)), "sha256": digest_, "count": self.count}

    def close(self) -> None:
        self.stream.close()
        self.temporary.unlink(missing_ok=True)


def _json_chunks(value: object) -> tuple[bytes, ...]:
    from unbake.project.cache import serialized

    if not isinstance(value, dict):
        return serialized("typemap.database", value), b"\n"
    chunks = [b"{"]
    for index, field in enumerate(sorted(value)):
        if index:
            chunks.append(b",")
        chunks.extend((json.dumps(field).encode() + b":", serialized("typemap.database." + field, value[field])))
    chunks.append(b"}\n")
    return tuple(chunks)


_json_stages: dict[Path, tuple[tuple[bytes, ...], str]] = {}
_json_installed: dict[Path, tuple[tuple[bytes, ...], tuple[int, int, int, int, int], str]] = {}


def _stage_json(path: Path, chunks: tuple[bytes, ...]) -> tuple[Path, str]:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise Held("solve", f"types.database: generated path is a symlink: {path}")
    hash_ = hashlib.sha256()
    descriptor, name = tempfile.mkstemp(prefix=".typemap-json-", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            for chunk in chunks:
                stream.write(chunk)
                hash_.update(chunk)
        digest_ = hash_.hexdigest()
        _json_stages[temporary] = chunks, digest_
        _file_digests[temporary] = _stamp(temporary), digest_
        return temporary, digest_
    except BaseException:
        temporary.unlink(missing_ok=True)
        _json_stages.pop(temporary, None)
        _file_digests.pop(temporary, None)
        raise


def stage_json(path: Path, value: object) -> Path:
    """Stage canonical JSON without a database-sized concatenation or deep copy."""
    return _stage_json(path, _json_chunks(value))[0]


def database_json(path: Path, value: object) -> tuple[Path | None, str]:
    """Skip staging only when both the serialized value and installed file match."""
    chunks = _json_chunks(value)
    installed = _json_installed.get(path)
    if installed is not None and installed[0] == chunks and not path.is_symlink():
        try:
            if installed[1] == _stamp(path):
                return None, installed[2]
        except OSError:
            pass
    return _stage_json(path, chunks)


def install(path: Path, staged: Path) -> None:
    os.replace(staged, path)
    record = _json_stages.pop(staged, None)
    _file_digests.pop(staged, None)
    if record is not None:
        chunks, digest_ = record
        stamp = _stamp(path)
        _json_installed[path] = chunks, stamp, digest_
        _file_digests[path] = stamp, digest_


def discard_json(staged: Path) -> None:
    staged.unlink(missing_ok=True)
    _json_stages.pop(staged, None)
    _file_digests.pop(staged, None)


_verified: dict[Path, tuple[tuple[int, int, int, int], str]] = {}


def verify_file(path: Path, expected: str, key: str) -> None:
    try:
        stat = path.stat()
        stamp = (stat.st_ino, stat.st_mtime_ns, stat.st_ctime_ns, stat.st_size)
        if _verified.get(path) == (stamp, expected):
            return
        if file_digest(path) != expected:
            raise Held("solve", f"{key}: content changed: {path}")
        _verified[path] = stamp, expected
    except OSError as error:
        raise Held("solve", f"{key}: {path}: {error}") from error
