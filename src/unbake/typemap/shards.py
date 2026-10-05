"""Compressed per-item/version facts with bounded, uncached iteration."""

from __future__ import annotations

import json
import sqlite3
import zlib
from collections.abc import Iterator, Mapping
from contextlib import closing
from pathlib import Path
from typing import Any

from unbake import atomic as atomic_files
from unbake import inputs
from unbake.config import Held


def pack(value: object) -> bytes:
    return zlib.compress(json.dumps(value, separators=(",", ":")).encode(), 1)


class Functions(Mapping[str, dict[str, Any]]):
    def __init__(self, path: Path, inventory: dict[str, Any]) -> None:
        self.path, self.inventory = path, inventory

    def __len__(self) -> int:
        return len(self.inventory)

    def __iter__(self) -> Iterator[str]:
        return iter(self.inventory)

    def __getitem__(self, name: str) -> dict[str, Any]:
        item = self.inventory[name]
        try:
            with closing(sqlite3.connect(f"file:{self.path}?mode=ro", uri=True)) as connection:
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
        wanted = set(names)
        bodies: dict[str, dict[str, Any]] = {name: {} for name in names}
        try:
            with closing(sqlite3.connect(f"file:{self.path}?mode=ro", uri=True)) as connection:
                connection.execute("PRAGMA cache_size=-2048")
                rows = connection.execute("SELECT name, version, body FROM functions ORDER BY name, version")
                for name, version, body in rows:
                    if name in wanted:
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
            with closing(sqlite3.connect(f"file:{self.path}?mode=ro", uri=True)) as connection:
                connection.execute("PRAGMA cache_size=-2048")
                row = connection.execute(
                    "SELECT body FROM functions WHERE name=? AND version=?", (name, version)
                ).fetchone()
                if row is None:
                    raise Held("solve", f"map.shards: missing containing version for {name}: {version}")
                body: dict[str, Any] = json.loads(zlib.decompress(row[0]))
        except (OSError, ValueError, zlib.error, sqlite3.Error) as error:
            raise Held("solve", f"map.shards: {self.path}: {error}") from error
        body.update(self.inventory[name]["versions"][version])
        return body


class Writer:
    """Keep the last complete map intact if any item cannot be mapped."""

    def __init__(self, directory: Path) -> None:
        import os
        import tempfile

        descriptor, name = tempfile.mkstemp(prefix=".facts-", suffix=".sqlite", dir=directory)
        os.close(descriptor)
        self.temporary = Path(name)
        self.connection = sqlite3.connect(name)
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
