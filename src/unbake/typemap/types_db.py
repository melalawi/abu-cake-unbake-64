"""build/types.sqlite: the type solution as per-symbol rows, its semantic summary and pending redraft marks.

Tables:
- meta(key, value): small top-level fields as JSON, plus `content_sha256`, the digest of the canonical solution,
  and `solution_sha256`, the same digest without the revision: equal for an identical solution solved again.
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
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from unbake.config import Held, Project

DB_SCHEMA = 1
REUSE_META = ("revision", "inference_key", "inference_receipts", "inference_publication")
_MISSING = object()

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


def solution_digest(encoded: Encoded) -> str:
    """content_digest without the revision, which every solve advances."""
    return content_digest({key: item for key, item in encoded.items() if key != "revision"})


def solution(file: Path) -> str | None:
    """The installed solution's digest; None for a missing database or one written before it was recorded."""
    if not file.is_file():
        return None
    with _connection(file) as connection:
        row = connection.execute("SELECT value FROM meta WHERE key = 'solution_sha256'").fetchone()
        return None if row is None else str(json.loads(row[0]))


def _connect(file: Path) -> sqlite3.Connection:
    try:
        return sqlite3.connect(f"file:{file}?mode=ro", uri=True)
    except sqlite3.Error as error:
        raise Held("types", f"types.sqlite: {file}: {error}") from error


@contextmanager
def _connection(file: Path) -> Iterator[sqlite3.Connection]:
    connection = _connect(file)
    try:
        yield connection
    except (sqlite3.Error, ValueError, KeyError, TypeError) as error:
        raise Held("types", f"types.sqlite: {file}: corrupt database: {error}") from error
    finally:
        connection.close()


def compatible(file: Path) -> bool:
    """Older storage contracts are stale; a broken current contract is corruption.

    user_version zero is the unversioned generation that omitted inference receipts.
    Check before every reuse path, including the solver's warm marker and step readiness.
    """
    if not file.is_file():
        return False
    with _connection(file) as connection:
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        if not {"meta", "entries"} <= tables:
            raise Held("types", f"types.sqlite: {file}: corrupt database: missing meta/entries tables")
        keys = (*REUSE_META, "schema")
        placeholders = ",".join("?" for _ in keys)
        metadata = {
            key: json.loads(value)
            for key, value in connection.execute(f"SELECT key, value FROM meta WHERE key IN ({placeholders})", keys)
        }
        if "schema" in metadata and type(metadata["schema"]) is not int:
            raise Held("types", f"types.sqlite: {file}: corrupt database: invalid meta schema")
        if version != DB_SCHEMA or metadata.get("schema", 1) != 1:
            return False
        for table in ("summary", "redraft"):
            if table not in tables:
                raise Held("types", f"types.sqlite: {file}: corrupt database: missing {table} table")
        for key in REUSE_META:
            if key not in metadata:
                raise Held("types", f"types.sqlite: {file}: corrupt database: missing meta {key}")
        return True


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
            connection.execute(f"PRAGMA user_version = {DB_SCHEMA}")
            for statement in SCHEMA:
                connection.execute(statement)
            meta = {key: item for key, item in encoded.items() if key not in KINDS}
            meta["content_sha256"] = _encode(digest)
            meta["solution_sha256"] = _encode(solution_digest(encoded))
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


def meta(file: Path, key: str, *, default: Any = _MISSING) -> Any:
    with _connection(file) as connection:
        row = connection.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        if row is None:
            if default is not _MISSING:
                return default
            raise Held("types", f"types.sqlite: {file}: corrupt database: missing meta {key}")
        return json.loads(row[0])


def entries(file: Path, kind: str, names: Iterable[str]) -> dict[str, Any]:
    """Only the named rows of KIND (the per-function context reads a handful, never the whole table)."""
    wanted = sorted(set(names))
    if not wanted:
        return {}
    with _connection(file) as connection:
        result = {}
        for start in range(0, len(wanted), 500):
            chunk = wanted[start : start + 500]
            marks = ",".join("?" * len(chunk))
            for name, value in connection.execute(
                f"SELECT name, value FROM entries WHERE kind = ? AND name IN ({marks})", (kind, *chunk)
            ):
                result[name] = json.loads(value)
        return result


def read(file: Path) -> dict[str, Any]:
    """The whole solution (the solver and the headers step); memoised by file identity."""
    stat = file.stat()
    stamp = (stat.st_ino, stat.st_mtime_ns, stat.st_size)
    cached = _full.get(file)
    if cached is not None and cached[0] == stamp:
        return cached[1]
    with _connection(file) as connection:
        value: dict[str, Any] = {key: json.loads(row) for key, row in connection.execute("SELECT key, value FROM meta")}
        value.pop("content_sha256", None)
        for kind in KINDS:
            value[kind] = {}
        for kind, name, row in connection.execute("SELECT kind, name, value FROM entries"):
            value[kind][name] = json.loads(row)
    _full[file] = (stamp, value)
    return value


def summary(file: Path) -> dict[str, Any]:
    with _connection(file) as connection:
        if not connection.execute(
            "SELECT 1 FROM sqlite_master WHERE name = ?", ("summary",)
        ).fetchone() and not compatible(file):
            return {}
        result: dict[str, Any] = {kind: {} for kind in KINDS if kind != "dependencies"}
        for kind, name, semantic, users in connection.execute("SELECT kind, name, semantic_sha256, users FROM summary"):
            result.setdefault(kind, {})[name] = {"semantic_sha256": semantic, "users": json.loads(users)}
        return result


def redrafts(file: Path) -> dict[str, Any]:
    with _connection(file) as connection:
        if not connection.execute(
            "SELECT 1 FROM sqlite_master WHERE name = ?", ("redraft",)
        ).fetchone() and not compatible(file):
            return {}
        return {
            function: json.loads(value) for function, value in connection.execute("SELECT function, value FROM redraft")
        }


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
