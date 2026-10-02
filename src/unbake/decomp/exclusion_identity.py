"""Refresh reserved item names from their measured ROM and RAM anchors."""

import json
from pathlib import Path
from typing import Any

from unbake.layout.split import Function
from unbake.project.config import Held, Project


def canonical(project: Project, names: list[str], rows: list[Function]) -> list[str]:
    available = {name for row in rows for name in (row.name, *row.aliases)}
    unknown = set(names) - available
    if not unknown:
        return names
    path = project.build / "setup/layout.json"
    if not path.is_file():
        return names
    try:
        evidence: dict[str, Any] = json.loads(path.read_bytes())
        if (
            evidence.get("schema") != 1
            or evidence.get("project_id") != project.id
            or evidence.get("workspace_id") != project.workspace_id
            or evidence.get("rom_sha1") != {v: project.version(v).baserom_sha1 for v in project.versions}
        ):
            return names
        resolved = {}
        for name in unknown:
            targets = set()
            for version in project.versions:
                owners = [row for row in evidence["versions"][version]["functions"] if row["name"] == name]
                if len(owners) > 1:
                    return names
                for owner in owners:
                    matches = [
                        row
                        for row in rows
                        if row.version == version
                        and row.start <= owner["start"] < row.end
                        and row.address + owner["start"] - row.start == owner["address"]
                    ]
                    if len(matches) != 1:
                        return names
                    targets.add(matches[0].name)
            if len(targets) != 1:
                return names
            resolved[name] = targets.pop()
        return list(dict.fromkeys(resolved.get(name, name) for name in names))
    except (KeyError, TypeError, ValueError, OSError):
        return names


def publish(project: Project, path: Path, before: bytes, names: list[str]) -> None:
    from unbake.match.common import atomic
    from unbake.project.build import lock

    with lock(project):
        if path.is_symlink() or path.read_bytes() != before:
            raise Held("exclusions", f"exclusions.stale: {path}: changed during item refresh")
        atomic(path, (json.dumps({"schema": 1, "functions": names}, indent=2) + "\n").encode())
