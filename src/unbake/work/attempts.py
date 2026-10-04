"""Attempt history: build/work/FUNC/attempts.jsonl per function, and the committed summary attempts.json.

Only the process that ran a compare appends its line, with a single O_APPEND write. The local logs live under
build/ and are never committed, so the progress report folds them into attempts.json at the project root
(write_summary). summaries() is the one reader of the folded history: the committed file merged with every
local log, so fuzzy progress and the ranker's history survive a fresh tree.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from unbake import atomic as atomic_files
from unbake.config import Held, Project

SUMMARY = "attempts.json"


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


@dataclass(frozen=True)
class Summary:
    """Folded history of one function: rounded so rewriting the same history gives the same bytes."""

    bytes: int
    best: dict[str, float]
    exact: bool
    minutes: float
    attempts: int

    def document(self) -> dict[str, Any]:
        return {
            "attempts": self.attempts,
            "best": dict(sorted(self.best.items())),
            "bytes": self.bytes,
            "exact": self.exact,
            "minutes": self.minutes,
        }

    @property
    def best_percent(self) -> float | None:
        return max(self.best.values(), default=None)


def summarize(rows: list[Attempt]) -> Summary:
    best: dict[str, float] = {}
    for row in rows:
        for version, result in row.versions.items():
            best[version] = max(best.get(version, 0.0), round(float(result["percent"]), 2))
    return Summary(rows[-1].bytes, best, any(row.exact for row in rows), round(minutes(rows), 2), len(rows))


def merge(committed: Summary | None, local: Summary | None) -> Summary:
    """Per version the higher best, exact if either is; size from the local log; effort never shrinks."""
    if committed is None or local is None:
        found = committed or local
        assert found is not None
        return found
    best = dict(committed.best)
    for version, percent in local.best.items():
        best[version] = max(best.get(version, 0.0), percent)
    return Summary(
        local.bytes,
        best,
        committed.exact or local.exact,
        max(committed.minutes, local.minutes),
        max(committed.attempts, local.attempts),
    )


def summary_path(project: Project) -> Path:
    return project.root / SUMMARY


def _committed(project: Project) -> dict[str, Summary]:
    target = summary_path(project)
    if not target.is_file():
        return {}
    try:
        document = json.loads(target.read_bytes())
        if document["v"] != 1:
            raise ValueError(f"v must be 1, not {document['v']!r}")
        return {
            str(name): Summary(
                int(value["bytes"]),
                {str(version): float(percent) for version, percent in value["best"].items()},
                bool(value["exact"]),
                float(value["minutes"]),
                int(value["attempts"]),
            )
            for name, value in document["functions"].items()
        }
    except (ValueError, KeyError, TypeError, AttributeError) as error:
        raise Held("work", f"{SUMMARY}: {target}: {error}") from error


def _logged(project: Project) -> list[str]:
    if not project.work.is_dir():
        return []
    return sorted(entry.name for entry in project.work.iterdir() if (entry / "attempts.jsonl").is_file())


def summaries(project: Project) -> dict[str, Summary]:
    """The committed attempts.json merged with every local attempts.jsonl. Every history reader uses this."""
    table = _committed(project)
    for function in _logged(project):
        rows = read(project, function)
        if rows:
            table[function] = merge(table.get(function), summarize(rows))
    return dict(sorted(table.items()))


def encode(table: dict[str, Summary]) -> bytes:
    functions = {name: table[name].document() for name in sorted(table)}
    return (json.dumps({"functions": functions, "v": 1}, indent=2, sort_keys=True) + "\n").encode()


def write_summary(project: Project, rows: set[str]) -> Path:
    """Fold the local logs into attempts.json, keeping only functions that are still rows of some version."""
    table = {name: summary for name, summary in summaries(project).items() if name in rows}
    target = summary_path(project)
    content = encode(table)
    if not target.is_file() or target.read_bytes() != content:
        atomic_files.write(target, content)
    return target
