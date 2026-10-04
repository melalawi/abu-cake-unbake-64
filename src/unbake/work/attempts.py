"""Attempt history of one function: build/work/FUNC/attempts.jsonl, one JSON line per compare.

Only the process that ran a compare appends its line, with a single O_APPEND write.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from unbake.config import Held, Project


@dataclass(frozen=True)
class Attempt:
    t: str
    function: str
    sha256: str
    bytes: int
    versions: dict[str, dict[str, Any]]
    best_percent: float
    exact: bool
    seconds: float
    compiler: str

    def document(self) -> dict[str, Any]:
        return {
            "t": self.t,
            "function": self.function,
            "sha256": self.sha256,
            "bytes": self.bytes,
            "versions": self.versions,
            "best_percent": self.best_percent,
            "exact": self.exact,
            "seconds": self.seconds,
            "compiler": self.compiler,
        }


def directory(project: Project, function: str) -> Path:
    return project.work / function


def path(project: Project, function: str) -> Path:
    return directory(project, function) / "attempts.jsonl"


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def append(project: Project, attempt: Attempt) -> None:
    target = path(project, attempt.function)
    target.parent.mkdir(parents=True, exist_ok=True)
    line = (json.dumps(attempt.document(), sort_keys=True) + "\n").encode()
    descriptor = os.open(target, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
    try:
        os.write(descriptor, line)
    finally:
        os.close(descriptor)


def read(project: Project, function: str) -> list[Attempt]:
    target = path(project, function)
    if not target.is_file():
        return []
    rows = []
    for number, line in enumerate(target.read_text().splitlines(), 1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
            rows.append(
                Attempt(
                    value["t"],
                    value["function"],
                    value["sha256"],
                    int(value["bytes"]),
                    dict(value["versions"]),
                    float(value["best_percent"]),
                    bool(value["exact"]),
                    float(value["seconds"]),
                    str(value["compiler"]),
                )
            )
        except (ValueError, KeyError, TypeError) as error:
            raise Held("work", f"attempts.{function}: {target}:{number}: {error}") from error
    return rows


def functions(project: Project) -> list[str]:
    """Functions with a work directory holding a draft."""
    if not project.work.is_dir():
        return []
    return sorted(entry.name for entry in project.work.iterdir() if (entry / f"{entry.name}.c").is_file())


def minutes(rows: list[Attempt]) -> float:
    """Wall minutes from the first attempt to the first exact one (or the last attempt)."""
    if not rows:
        return 0.0
    first = datetime.fromisoformat(rows[0].t)
    stop = next((row for row in rows if row.exact), rows[-1])
    spent = (datetime.fromisoformat(stop.t) - first).total_seconds() + stop.seconds
    return max(spent / 60.0, 1.0 / 60.0)
