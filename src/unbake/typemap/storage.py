"""Pinned project evidence and atomic generated records."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any

from unbake import inputs
from unbake.config import Held, Project
from unbake.project_tools import atomic as atomic_files


def digest(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def encoded(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise Held("solve", f"types.database: generated path is a symlink: {path}")
    atomic_files.write(path, content)


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
        "rom_sha1": {v: project.version(v).baserom_sha1 for v in project.versions},
    }


def validate_identity(project: Project, value: dict[str, Any], key: str) -> None:
    if any(value.get(field) != expected for field, expected in identity(project).items()):
        raise Held("solve", f"{key}: project/ROM identity changed")


def map_inputs(project: Project) -> dict[str, str]:
    paths = {project.root / "config.toml"}
    symbol_inputs: dict[str, str] = {}
    for version in project.versions:
        configured = project.version(version)
        if not configured.baserom.is_file():
            raise Held("map", f"map.rom_sha1.{version}: missing {configured.baserom}")
        paths.add(configured.baserom)
        for filename in ("splat_symbols.csv", "symbol-addresses.txt"):
            generated_symbols = project.build_link(version) / filename
            if generated_symbols.is_file():
                # Splat rewrites extraction details such as size/defined flags
                # after a build. Mapping consumes only names and addresses.
                symbol_inputs[str(generated_symbols.relative_to(project.root))] = symbol_digest(generated_symbols)
        for key, path in (("split", configured.split), ("symbols", configured.symbols)):
            if not path.is_file():
                raise Held("map", f"map.{key}.{version}: missing {path}")
            paths.add(path)
    # Instruction facts come directly from the pinned ROM and split intervals.
    # Extracted assembly is a disposable rendering: make prunes obsolete C-unit
    # assembly and rewrites other extraction outputs without changing the ROM.
    paths.update(path for path in (project.build / "setup/layout.json",) if path.is_file())
    result = {str(path.relative_to(project.root)): inputs.digest(path) for path in sorted(paths)}
    result.update(symbol_inputs)
    return result


_symbol_digests: dict[Path, tuple[str, str]] = {}


def symbol_digest(path: Path) -> str:
    """Pin exactly the extraction symbol facts consumed by map_program."""
    from unbake.project_tools.extract import discovered_symbols, symbols_from

    content = inputs.digest(path)
    cached = _symbol_digests.get(path)
    if cached is not None and cached[0] == content:
        return cached[1]
    try:
        symbols = discovered_symbols(path, {}) if path.name == "splat_symbols.csv" else symbols_from([path])
    except (OSError, ValueError, KeyError) as error:
        raise Held("map", f"map.symbols: {path}: {error}") from error
    result = digest(encoded(symbols))
    _symbol_digests[path] = content, result
    return result


def generated(project: Project, path: Path) -> bool:
    from unbake.layout import index

    return path in index.headers(project)


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
        digest_ = inputs.digest(self.temporary)
        path = self.temporary.parent / ("constraints-" + digest_ + ".jsonl")
        atomic_files.publish(self.temporary, path)
        return {"kind": "shard", "path": str(path.relative_to(root)), "sha256": digest_, "count": self.count}

    def close(self) -> None:
        self.stream.close()
        self.temporary.unlink(missing_ok=True)


def _json_chunks(value: object) -> tuple[bytes, ...]:
    from unbake.cache import serialized

    if not isinstance(value, dict):
        return serialized(value), b"\n"
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
        inputs._digests[temporary] = inputs.signature(temporary), digest_
        return temporary, digest_
    except BaseException:
        temporary.unlink(missing_ok=True)
        _json_stages.pop(temporary, None)
        inputs._digests.pop(temporary, None)
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
            if installed[1] == inputs.signature(path):
                return None, installed[2]
        except OSError:
            pass
    return _stage_json(path, chunks)


def install(path: Path, staged: Path) -> None:
    atomic_files.publish(staged, path)
    record = _json_stages.pop(staged, None)
    inputs._digests.pop(staged, None)
    if record is not None:
        chunks, digest_ = record
        stamp = inputs.signature(path)
        _json_installed[path] = chunks, stamp, digest_
        inputs._digests[path] = stamp, digest_


def discard_json(staged: Path) -> None:
    staged.unlink(missing_ok=True)
    _json_stages.pop(staged, None)
    inputs._digests.pop(staged, None)


_verified: dict[Path, tuple[tuple[int, int, int, int], str]] = {}


def verify_file(path: Path, expected: str, key: str) -> None:
    try:
        stat = path.stat()
        stamp = (stat.st_ino, stat.st_mtime_ns, stat.st_ctime_ns, stat.st_size)
        if _verified.get(path) == (stamp, expected):
            return
        if inputs.digest(path) != expected:
            raise Held("solve", f"{key}: content changed: {path}")
        _verified[path] = stamp, expected
    except OSError as error:
        raise Held("solve", f"{key}: {path}: {error}") from error
