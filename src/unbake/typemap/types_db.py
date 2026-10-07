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
from typing import Any, cast

from unbake import cache as retention
from unbake import effort, sqlite, strict_json
from unbake.config import Held, Project
from unbake.process import capture
from unbake.process import named as cause_named

DB_SCHEMA = 2
REUSE_META = ("revision", "inference_key", "inference_receipts", "inference_publication")
_MISSING = object()

KINDS = ("functions", "globals", "structs", "arrays", "dependencies")
SCHEMA = (
    "CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
    "CREATE TABLE entries (kind TEXT NOT NULL, name TEXT NOT NULL, value TEXT NOT NULL, PRIMARY KEY (kind, name))",
    "CREATE TABLE summary (kind TEXT NOT NULL, name TEXT NOT NULL, semantic_sha256 TEXT NOT NULL,"
    " users TEXT NOT NULL, PRIMARY KEY (kind, name))",
)


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
        return sqlite.connect(f"file:{file}?mode=ro", uri=True)
    except sqlite3.Error as error:
        raise Held(
            capture(
                error,
                cause=cause_named(
                    "types.sqlite", f"types.sqlite: {file}: {error}", owner="typemap.types_db", stage="types"
                ),
            )
        ) from error


@contextmanager
def _connection(file: Path) -> Iterator[sqlite3.Connection]:
    connection = _connect(file)
    try:
        yield connection
    except (sqlite3.Error, ValueError, KeyError, TypeError) as error:
        raise Held(
            capture(
                error,
                cause=cause_named(
                    "types.sqlite",
                    f"types.sqlite: {file}: corrupt database: {error}",
                    owner="typemap.types_db",
                    stage="types",
                ),
            )
        ) from error
    finally:
        connection.close()


def legacy_storage(file: Path) -> int:
    """Identify the old writer by its complete table contract, not PRAGMA alone.

    The pre-versioned writer stored semantic schema 1 in meta and did not set
    user_version. This is migration intake only; it never authorizes reuse.
    """
    expected = {
        "meta": (("key", "TEXT", 0, 1), ("value", "TEXT", 1, 0)),
        "entries": (("kind", "TEXT", 1, 1), ("name", "TEXT", 1, 2), ("value", "TEXT", 1, 0)),
        "summary": (
            ("kind", "TEXT", 1, 1),
            ("name", "TEXT", 1, 2),
            ("semantic_sha256", "TEXT", 1, 0),
            ("users", "TEXT", 1, 0),
        ),
        "redraft": (("function", "TEXT", 0, 1), ("value", "TEXT", 1, 0)),
    }
    with _connection(file) as connection:
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if version == DB_SCHEMA:
            expected.pop("redraft")
        valid = version in (0, 1, DB_SCHEMA) and tables == expected.keys()
        for table, columns in expected.items():
            actual = tuple(
                (row[1], row[2], row[3], row[5]) for row in connection.execute(f"PRAGMA table_info({table})")
            )
            valid = valid and actual == columns
        metadata = {
            key: strict_json.loads(value, file) for key, value in connection.execute("SELECT key,value FROM meta")
        }
        valid = valid and type(metadata.get("schema")) is int and metadata["schema"] == 1
        valid = valid and type(metadata.get("revision")) is int and metadata["revision"] >= 0
        import re

        valid = valid and all(
            isinstance(metadata.get(key), str) and re.fullmatch(r"[0-9a-f]{64}", metadata[key])
            for key in ("content_sha256", "solution_sha256")
        )
        if not valid:
            raise Held(
                cause_named(
                    "migration.storage",
                    f"{file}: unknown legacy storage shape/metadata (user_version {version})",
                    owner="typemap.types_db",
                    stage="migration",
                )
            )
        if version == DB_SCHEMA:
            compatible(file)
        return int(version)


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
            raise Held(
                cause_named(
                    "types.sqlite",
                    f"types.sqlite: {file}: corrupt database: missing meta/entries tables",
                    owner="typemap.types_db",
                    stage="types",
                )
            )
        keys = (*REUSE_META, "schema", "migration_reuse")
        placeholders = ",".join("?" for _ in keys)
        metadata = {
            key: json.loads(value)
            for key, value in connection.execute(f"SELECT key, value FROM meta WHERE key IN ({placeholders})", keys)
        }
        if "schema" in metadata and type(metadata["schema"]) is not int:
            raise Held(
                cause_named(
                    "types.sqlite",
                    f"types.sqlite: {file}: corrupt database: invalid meta schema",
                    owner="typemap.types_db",
                    stage="types",
                )
            )
        if version != DB_SCHEMA or metadata.get("schema", 1) != 1:
            return False
        for table in ("summary",):
            if table not in tables:
                raise Held(
                    cause_named(
                        "types.sqlite",
                        f"types.sqlite: {file}: corrupt database: missing {table} table",
                        owner="typemap.types_db",
                        stage="types",
                    )
                )
        if metadata.get("migration_reuse") == "unverified":
            return False
        for key in REUSE_META:
            if key not in metadata:
                raise Held(
                    cause_named(
                        "types.sqlite",
                        f"types.sqlite: {file}: corrupt database: missing meta {key}",
                        owner="typemap.types_db",
                        stage="types",
                    )
                )
        return True


def _sync(
    connection: sqlite3.Connection,
    table: str,
    columns: tuple[str, ...],
    keys: int,
    desired: dict[tuple[str, ...], tuple[str, ...]],
) -> None:
    """Compare stored encoded rows, writing only new/changed/deleted values in the staged copy."""
    before = {
        tuple(row[:keys]): tuple(row[keys:])
        for row in connection.execute("SELECT " + ",".join(columns) + " FROM " + table)
    }
    removed = sorted(before.keys() - desired.keys())
    changed = sorted(name for name, value in desired.items() if before.get(name) != value)
    if removed:
        where = " AND ".join(column + "=?" for column in columns[:keys])
        connection.executemany("DELETE FROM " + table + " WHERE " + where, removed)
    if changed:
        marks = ",".join("?" for _ in columns)
        connection.executemany(
            "INSERT OR REPLACE INTO " + table + " VALUES (" + marks + ")", (name + desired[name] for name in changed)
        )
    effort.count("types.publish." + table, len(changed) + len(removed), len(before.keys() | desired.keys()))


def stage(
    destination: Path,
    encoded: Encoded,
    summary: dict[str, dict[str, dict[str, Any]]],
) -> tuple[Path, str]:
    """Write a complete encoded solution next to DESTINATION; return (staged file, content digest)."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=".types-", suffix=".sqlite", dir=destination.parent)
    os.close(descriptor)
    staged = Path(name)
    digest = content_digest(encoded)
    connection: sqlite3.Connection | None = None
    try:
        staged.unlink()
        connection = sqlite.connect(staged)
        retained = destination.is_file() and compatible(destination)
        if retained:
            with _connection(destination) as source:
                source.backup(connection)
        with connection:
            if not retained:
                connection.execute(f"PRAGMA user_version = {DB_SCHEMA}")
                for statement in SCHEMA:
                    connection.execute(statement)
            meta = {key: item for key, item in encoded.items() if key not in KINDS}
            meta["content_sha256"] = _encode(digest)
            meta["solution_sha256"] = _encode(solution_digest(encoded))
            if any(not isinstance(item, str) for item in meta.values()):
                raise Held(
                    cause_named(
                        "types.sqlite",
                        "types.sqlite: expected encoded metadata strings",
                        owner="typemap.types_db",
                        stage="types",
                    )
                )
            _sync(connection, "meta", ("key", "value"), 1, {(key,): (cast(str, item),) for key, item in meta.items()})
            entries: dict[tuple[str, ...], tuple[str, ...]] = {}
            for kind in KINDS:
                rows = encoded.get(kind, {})
                if not isinstance(rows, dict):
                    raise Held(
                        cause_named(
                            "types.sqlite",
                            f"types.sqlite: {kind}: expected rows by name",
                            owner="typemap.types_db",
                            stage="types",
                        )
                    )
                entries.update({(kind, name): (value,) for name, value in rows.items()})
            _sync(connection, "entries", ("kind", "name", "value"), 2, entries)
            _sync(
                connection,
                "summary",
                ("kind", "name", "semantic_sha256", "users"),
                2,
                {
                    (kind, name): (row["semantic_sha256"], _encode(row["users"]))
                    for kind, rows in summary.items()
                    for name, row in rows.items()
                },
            )
    except BaseException:
        staged.unlink(missing_ok=True)
        raise
    finally:
        if connection is not None:
            connection.close()
    return staged, digest


def install(destination: Path, staged: Path) -> None:
    from unbake import atomic

    atomic.publish(staged, destination)


def meta(file: Path, key: str, *, default: Any = _MISSING) -> Any:
    with _connection(file) as connection:
        row = connection.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        if row is None:
            if default is not _MISSING:
                return default
            raise Held(
                cause_named(
                    "types.sqlite",
                    f"types.sqlite: {file}: corrupt database: missing meta {key}",
                    owner="typemap.types_db",
                    stage="types",
                )
            )
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
    from unbake import inputs

    content = inputs.digest(file, algorithm="sha256", reuse=retention.configured())
    return retention.memo(
        "types-db", (str(file), content), lambda: _read(file), size=retention.memory_size, copy_out=retention.clone
    )


def _read(file: Path) -> dict[str, Any]:
    """The whole solution (the solver and the headers step); memoised by file identity."""
    with _connection(file) as connection:
        value: dict[str, Any] = {key: json.loads(row) for key, row in connection.execute("SELECT key, value FROM meta")}
        value.pop("content_sha256", None)
        for kind in KINDS:
            value[kind] = {}
        for kind, name, row in connection.execute("SELECT kind, name, value FROM entries"):
            value[kind][name] = json.loads(row)
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
