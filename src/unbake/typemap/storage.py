"""Pinned project evidence and atomic generated records."""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

from unbake import atomic as atomic_files
from unbake import cache as retention
from unbake import inputs
from unbake.config import Held, Project
from unbake.process import capture
from unbake.process import named as cause_named


def encoded(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def output(path: Path) -> Path:
    if path.is_symlink():
        raise Held(
            cause_named(
                "types.database",
                f"types.database: generated path is a symlink: {path}",
                owner="typemap.storage",
                stage="solve",
            )
        )
    return path


def read(path: Path, key: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_bytes())
    except (OSError, ValueError) as error:
        raise Held(
            capture(
                error, cause=cause_named(f"{key}", f"{key}: {path}: {error}", owner="typemap.storage", stage="solve")
            )
        ) from error
    if not isinstance(value, dict) or value.get("schema") != 1:
        raise Held(cause_named(f"{key}", f"{key}: schema=1 object required", owner="typemap.storage", stage="solve"))
    return value


def identity(project: Project) -> dict[str, Any]:
    return {
        "schema": 1,
        "project_id": project.id,
        "rom_sha1": {v: project.version(v).baserom_sha1 for v in project.versions},
    }


def validate_identity(project: Project, value: dict[str, Any], key: str) -> None:
    if any(value.get(field) != expected for field, expected in identity(project).items()):
        raise Held(
            cause_named(f"{key}", f"{key}: project/ROM identity changed", owner="typemap.storage", stage="solve")
        )


def map_inputs(project: Project) -> dict[str, str]:
    paths = {project.root / "config.toml"}
    symbol_inputs: dict[str, str] = {}
    for version in project.versions:
        configured = project.version(version)
        if not configured.baserom.is_file():
            raise Held(
                cause_named(
                    f"map.rom_sha1.{version}",
                    f"map.rom_sha1.{version}: missing {configured.baserom}",
                    owner="typemap.storage",
                    stage="map",
                )
            )
        paths.add(configured.baserom)
        for filename in ("splat_symbols.csv", "symbol-addresses.txt"):
            generated_symbols = project.build_link(version) / filename
            if generated_symbols.is_file():
                # Splat rewrites extraction details such as size/defined flags
                # after a build. Mapping consumes only names and addresses.
                symbol_inputs[str(generated_symbols.relative_to(project.root))] = symbol_digest(generated_symbols)
        for key, path in (("split", configured.split), ("symbols", configured.symbols)):
            if not path.is_file():
                raise Held(
                    cause_named(
                        f"map.{key}.{version}",
                        f"map.{key}.{version}: missing {path}",
                        owner="typemap.storage",
                        stage="map",
                    )
                )
            paths.add(path)
    # Instruction facts come directly from the pinned ROM and split intervals.
    # Extracted assembly is a disposable rendering: make prunes obsolete C-unit
    # assembly and rewrites other extraction outputs without changing the ROM.
    paths.update(path for path in (project.build / "setup/layout.json",) if path.is_file())
    result = {
        str(path.relative_to(project.root)): inputs.digest(path, algorithm="sha256", reuse=retention.configured())
        for path in sorted(paths)
    }
    result.update(symbol_inputs)
    return result


def symbol_digest(path: Path) -> str:
    """Pin exactly the extraction symbol facts consumed by map_program."""
    from unbake.extract import discovered_symbols, read_symbol_table

    content = inputs.digest(path, algorithm="sha256", reuse=retention.configured())

    def parse() -> str:
        try:
            symbols = discovered_symbols(path, {}) if path.name == "splat_symbols.csv" else read_symbol_table(path)
        except (OSError, ValueError, KeyError) as error:
            raise Held(
                capture(
                    error,
                    cause=cause_named(
                        "map.symbols", f"map.symbols: {path}: {error}", owner="typemap.storage", stage="map"
                    ),
                )
            ) from error
        return inputs.bytes_digest(encoded(symbols), algorithm="sha256")

    return retention.memo("symbol-digest", (path.name, content), parse, size=retention.memory_size, copy_out=str)


def generated_view(project: Project) -> Callable[[Path], bool]:
    """One ownership catalogue per read operation, including a missing-index recovery scan."""
    from unbake.layout import index

    listed = index.listed(project)

    def contains(path: Path) -> bool:
        return path.is_file() and (path in listed or any(index.marked(path, root) for root in project.include))

    return contains


def generated(project: Project, path: Path) -> bool:
    """Classify one path; loops use generated_view so recovery is not repeated per file."""
    return generated_view(project)(path)


def relocatable(value: Any, root: Path) -> Any:
    """VALUE with every str or Path under ROOT spelled relative to it, so a moved tree keys the same."""
    if isinstance(value, dict):
        return {key: relocatable(item, root) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [relocatable(item, root) for item in value]
    if isinstance(value, Path):
        return relative_root(root, value) if value.is_relative_to(root) else str(value)
    if isinstance(value, str) and value.startswith(str(root) + os.sep):
        return relative_root(root, Path(value))
    return value


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
        digest_ = inputs.digest(self.temporary, algorithm="sha256", reuse=retention.configured())
        path = self.temporary.parent / ("constraints-" + digest_ + ".jsonl")
        # The shard is re-derivable from the map; its loss costs a recompute, never a wrong answer.
        atomic_files.publish(self.temporary, path, durable=False)
        return {"kind": "shard", "path": str(path.relative_to(root)), "sha256": digest_, "count": self.count}

    def close(self) -> None:
        self.stream.close()
        self.temporary.unlink(missing_ok=True)


def verify_file(path: Path, expected: str, key: str) -> None:
    try:
        if inputs.digest(path, algorithm="sha256", reuse=retention.configured()) != expected:
            raise Held(cause_named(f"{key}", f"{key}: content changed: {path}", owner="typemap.storage", stage="solve"))
    except OSError as error:
        raise Held(
            capture(
                error, cause=cause_named(f"{key}", f"{key}: {path}: {error}", owner="typemap.storage", stage="solve")
            )
        ) from error
