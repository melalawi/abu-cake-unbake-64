"""Compressed per-item/version facts with bounded, uncached iteration."""

from __future__ import annotations

import json
import sqlite3
import threading
import zlib
from collections.abc import Iterator, Mapping
from contextlib import closing
from pathlib import Path
from typing import Any

from unbake import atomic as atomic_files
from unbake import inputs, sqlite
from unbake.config import Held

# Names per `WHERE name IN (...)` read (SQLite's variable limit is far above this).
READ_BATCH = 500


def pack(value: object) -> bytes:
    return zlib.compress(json.dumps(value, separators=(",", ":")).encode(), 1)


class Functions(Mapping[str, dict[str, Any]]):
    def __init__(self, path: Path, inventory: dict[str, Any]) -> None:
        self.path, self.inventory = path, inventory
        # One read-only connection per thread for version(): a map refresh reads thousands of bodies one by one.
        self._local = threading.local()

    def _connection(self) -> sqlite3.Connection:
        connection: sqlite3.Connection | None = getattr(self._local, "connection", None)
        if connection is None:
            connection = sqlite.connect(f"file:{self.path}?mode=ro", uri=True)
            connection.execute("PRAGMA cache_size=-2048")
            self._local.connection = connection
        return connection

    def __getstate__(self) -> dict[str, Any]:
        return {"path": self.path, "inventory": self.inventory}

    def __setstate__(self, state: dict[str, Any]) -> None:
        self.__init__(state["path"], state["inventory"])  # type: ignore[misc]

    def __len__(self) -> int:
        return len(self.inventory)

    def __iter__(self) -> Iterator[str]:
        return iter(self.inventory)

    def __getitem__(self, name: str) -> dict[str, Any]:
        item = self.inventory[name]
        try:
            with closing(sqlite.connect(f"file:{self.path}?mode=ro", uri=True)) as connection:
                connection.execute("PRAGMA cache_size=-2048")
                rows = connection.execute("SELECT version, body FROM functions WHERE name=? ORDER BY version", (name,))
                versions = {version: json.loads(zlib.decompress(body)) for version, body in rows}
        except (OSError, ValueError, zlib.error, sqlite3.Error) as error:
            raise Held("solve", f"map.shards: {self.path}: {error}") from error
        if set(versions) != set(item["versions"]):
            raise Held("solve", f"map.shards: missing containing version for {name}")
        for version, body in versions.items():
            body.update(item["versions"][version])
        return {"aliases": item["aliases"], "versions": versions}

    def read(self, names: list[str]) -> dict[str, dict[str, Any]]:
        """Complete items of NAMES, in that order, over one connection (equal to item-by-item lookup)."""
        bodies: dict[str, dict[str, Any]] = {name: {} for name in names}
        try:
            with closing(sqlite.connect(f"file:{self.path}?mode=ro", uri=True)) as connection:
                connection.execute("PRAGMA cache_size=-2048")
                for start in range(0, len(names), READ_BATCH):
                    batch = names[start : start + READ_BATCH]
                    rows = connection.execute(
                        (
                            "SELECT name, version, body FROM functions WHERE name IN ("
                            + ",".join("?" * len(batch))
                            + ") ORDER BY name, version"
                        ),
                        batch,
                    )
                    for name, version, body in rows:
                        bodies[name][version] = json.loads(zlib.decompress(body))
        except (OSError, ValueError, zlib.error, sqlite3.Error) as error:
            raise Held("solve", f"map.shards: {self.path}: {error}") from error
        items = {}
        for name in names:
            item, versions = self.inventory[name], bodies[name]
            if set(versions) != set(item["versions"]):
                raise Held("solve", f"map.shards: missing containing version for {name}")
            for version, body in versions.items():
                body.update(item["versions"][version])
            items[name] = {"aliases": item["aliases"], "versions": versions}
        return items

    def version(self, name: str, version: str) -> dict[str, Any]:
        """Read one containing body without decompressing its other versions."""
        try:
            row = (
                self._connection()
                .execute("SELECT body FROM functions WHERE name=? AND version=?", (name, version))
                .fetchone()
            )
            if row is None:
                raise Held("solve", f"map.shards: missing containing version for {name}: {version}")
            body: dict[str, Any] = json.loads(zlib.decompress(row[0]))
        except (OSError, ValueError, zlib.error, sqlite3.Error) as error:
            raise Held("solve", f"map.shards: {self.path}: {error}") from error
        body.update(self.inventory[name]["versions"][version])
        return body


def bodies(functions: Mapping[str, dict[str, Any]], names: list[str]) -> dict[str, dict[str, Any]]:
    """The complete items of NAMES: one batched read of a shard, else lookups in the mapping."""
    if isinstance(functions, Functions):
        return functions.read(names)
    return {name: functions[name] for name in names}


def chunks(names: list[str], workers: int) -> list[list[str]]:
    """NAMES in order, in pieces a worker reads in one batch: about a few pieces per worker."""
    from unbake import pool

    size = pool.width(len(names), workers)
    return [names[start : start + size] for start in range(0, len(names), size)]


class Writer:
    """Keep the last complete map intact if any item cannot be mapped."""

    def __init__(self, directory: Path) -> None:
        import os
        import tempfile

        descriptor, name = tempfile.mkstemp(prefix=".facts-", suffix=".sqlite", dir=directory)
        os.close(descriptor)
        self.temporary = Path(name)
        self.connection = sqlite.connect(name)
        self.connection.execute("PRAGMA cache_size=-2048")
        self.connection.execute(
            "CREATE TABLE functions (name TEXT, version TEXT, body BLOB, PRIMARY KEY(name,version))"
        )

    def add(self, name: str, version: str, body: dict[str, Any]) -> None:
        self.connection.execute("INSERT INTO functions VALUES (?,?,?)", (name, version, pack(body)))

    def finish(self) -> Path:

        self.connection.commit()
        self.connection.close()
        path = self.temporary.parent / ("facts-" + inputs.digest(self.temporary) + ".sqlite")
        atomic_files.publish(self.temporary, path)
        return path

    def close(self) -> None:
        self.connection.close()
        self.temporary.unlink(missing_ok=True)
