"""build/types.sqlite: the type solution as per-symbol rows, its semantic summary and pending redraft marks.

Tables:
- meta(key, value): small top-level fields as JSON, plus `content_sha256`, the digest of the canonical solution.
- entries(kind, name, value): one row per function/global/struct/array/dependency entry.
- summary(kind, name, semantic_sha256, users): the bounded index the solver diffs against.
- redraft(function, value): marks for drafts made against an older solution.
A new solution is written to a temporary file and renamed into place, so readers never see a partial file.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import tempfile
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from unbake.config import Held, Project

KINDS = ("functions", "globals", "structs", "arrays", "dependencies")
SCHEMA = (
    "CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
    "CREATE TABLE entries (kind TEXT NOT NULL, name TEXT NOT NULL, value TEXT NOT NULL, PRIMARY KEY (kind, name))",
    "CREATE TABLE summary (kind TEXT NOT NULL, name TEXT NOT NULL, semantic_sha256 TEXT NOT NULL,"
    " users TEXT NOT NULL, PRIMARY KEY (kind, name))",
    "CREATE TABLE redraft (function TEXT PRIMARY KEY, value TEXT NOT NULL)",
)

_full: dict[Path, tuple[tuple[int, int, int], dict[str, Any]]] = {}


def path(project: Project) -> Path:
    return project.build / "types.sqlite"


def _encode(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


Encoded = dict[str, "str | dict[str, str]"]


def encode(value: dict[str, Any]) -> Encoded:
    """Every top-level value and every row of each kind as canonical JSON, encoded once per solution."""
    return {
        key: {name: _encode(row) for name, row in item.items()}
        if key in KINDS and isinstance(item, dict)
        else _encode(item)
        for key, item in value.items()
    }


def content_digest(encoded: Encoded) -> str:
    """The canonical digest of an encoded solution (independent of sqlite page layout)."""
    hash_ = hashlib.sha256()
    for key in sorted(encoded):
        hash_.update(_encode(key).encode() + b":")
        item = encoded[key]
        if isinstance(item, dict):
            for name in sorted(item):
                hash_.update(_encode(name).encode() + b"=" + item[name].encode() + b";")
        else:
            hash_.update(item.encode())
        hash_.update(b"\n")
    return hash_.hexdigest()


def _connect(file: Path) -> sqlite3.Connection:
    try:
        return sqlite3.connect(f"file:{file}?mode=ro", uri=True)
    except sqlite3.Error as error:
        raise Held("types", f"types.sqlite: {file}: {error}") from error


def stage(
    destination: Path,
    encoded: Encoded,
    summary: dict[str, dict[str, dict[str, Any]]],
    marks: dict[str, Any],
) -> tuple[Path, str]:
    """Write a complete encoded solution next to DESTINATION; return (staged file, content digest)."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=".types-", suffix=".sqlite", dir=destination.parent)
    os.close(descriptor)
    staged = Path(name)
    digest = content_digest(encoded)
    try:
        staged.unlink()
        connection = sqlite3.connect(staged)
        with connection:
            for statement in SCHEMA:
                connection.execute(statement)
            meta = {key: item for key, item in encoded.items() if key not in KINDS}
            meta["content_sha256"] = _encode(digest)
            connection.executemany("INSERT INTO meta VALUES (?, ?)", sorted(meta.items()))
            for kind in KINDS:
                rows = encoded.get(kind, {})
                if not isinstance(rows, dict):
                    raise Held("types", f"types.sqlite: {kind}: expected rows by name")
                connection.executemany(
                    "INSERT INTO entries VALUES (?, ?, ?)", ((kind, k, v) for k, v in sorted(rows.items()))
                )
            for kind, summaries in summary.items():
                connection.executemany(
                    "INSERT INTO summary VALUES (?, ?, ?, ?)",
                    ((kind, k, row["semantic_sha256"], _encode(row["users"])) for k, row in sorted(summaries.items())),
                )
            connection.executemany(
                "INSERT INTO redraft VALUES (?, ?)", ((k, _encode(v)) for k, v in sorted(marks.items()))
            )
        connection.close()
    except BaseException:
        staged.unlink(missing_ok=True)
        raise
    return staged, digest


def install(destination: Path, staged: Path) -> None:
    os.replace(staged, destination)
    _full.pop(destination, None)


def meta(file: Path, key: str) -> Any:
    connection = _connect(file)
    try:
        row = connection.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    finally:
        connection.close()
    if row is None:
        raise Held("types", f"types.sqlite: {file}: missing meta {key}")
    return json.loads(row[0])


def entries(file: Path, kind: str, names: Iterable[str]) -> dict[str, Any]:
    """Only the named rows of KIND (the per-function context reads a handful, never the whole table)."""
    wanted = sorted(set(names))
    if not wanted:
        return {}
    connection = _connect(file)
    try:
        result = {}
        for start in range(0, len(wanted), 500):
            chunk = wanted[start : start + 500]
            marks = ",".join("?" * len(chunk))
            for name, value in connection.execute(
                f"SELECT name, value FROM entries WHERE kind = ? AND name IN ({marks})", (kind, *chunk)
            ):
                result[name] = json.loads(value)
        return result
    finally:
        connection.close()


def read(file: Path) -> dict[str, Any]:
    """The whole solution (the solver and the headers step); memoised by file identity."""
    stat = file.stat()
    stamp = (stat.st_ino, stat.st_mtime_ns, stat.st_size)
    cached = _full.get(file)
    if cached is not None and cached[0] == stamp:
        return cached[1]
    connection = _connect(file)
    try:
        value: dict[str, Any] = {key: json.loads(row) for key, row in connection.execute("SELECT key, value FROM meta")}
        value.pop("content_sha256", None)
        for kind in KINDS:
            value[kind] = {}
        for kind, name, row in connection.execute("SELECT kind, name, value FROM entries"):
            value[kind][name] = json.loads(row)
    finally:
        connection.close()
    _full[file] = (stamp, value)
    return value


def summary(file: Path) -> dict[str, Any]:
    connection = _connect(file)
    try:
        result: dict[str, Any] = {kind: {} for kind in KINDS if kind != "dependencies"}
        for kind, name, semantic, users in connection.execute("SELECT kind, name, semantic_sha256, users FROM summary"):
            result.setdefault(kind, {})[name] = {"semantic_sha256": semantic, "users": json.loads(users)}
        return result
    finally:
        connection.close()


def redrafts(file: Path) -> dict[str, Any]:
    connection = _connect(file)
    try:
        return {
            function: json.loads(value) for function, value in connection.execute("SELECT function, value FROM redraft")
        }
    finally:
        connection.close()


def set_redrafts(file: Path, marks: dict[str, Any]) -> None:
    connection = sqlite3.connect(file)
    try:
        with connection:
            connection.execute("DELETE FROM redraft")
            connection.executemany(
                "INSERT INTO redraft VALUES (?, ?)", ((k, _encode(v)) for k, v in sorted(marks.items()))
            )
    finally:
        connection.close()
