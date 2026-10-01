"""Pinned project evidence and atomic generated records."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any

from unbake.project.config import Held, Project


def digest(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def encoded(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, indent=2) + "\n").encode()


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


def inputs(project: Project, *, headers: bool = False) -> dict[str, str]:
    paths = {project.root / "config.toml"}
    for version in project.versions:
        configured = project.version(version)
        if not configured.baserom.is_file():
            raise Held("map", f"map.rom_sha1.{version}: missing {configured.baserom}")
        paths.add(configured.baserom)
        for key, path in (("split", configured.split), ("symbols", configured.symbols)):
            if not path.is_file():
                raise Held("map", f"map.{key}.{version}: missing {path}")
            paths.add(path)
    paths.update(
        path
        for path in (project.build / "setup/layout.json", project.root / "docs/setup/layout.json")
        if path.is_file()
    )
    if headers:
        paths.update(path for root in project.include for path in root.rglob("*.h") if not generated(project, path))
        proven = project.build / "types/proven.json"
        if proven.is_file():
            paths.add(proven)
            for row in read(proven, "types.feedback").get("records", {}).values():
                source = project.root / row["source"]
                if not source.is_file() or digest(source.read_bytes()) != row["source_sha256"]:
                    raise Held("solve", f"types.feedback.source_sha256: published source changed: {source}")
                paths.add(source)
    return {str(path.relative_to(project.root)): digest(path.read_bytes()) for path in sorted(paths)}


def generated(project: Project, path: Path) -> bool:
    return bool(project.include) and path in (
        project.include[0] / "shared/typemap.h",
        project.include[0] / "shared/prototypes.h",
    )
