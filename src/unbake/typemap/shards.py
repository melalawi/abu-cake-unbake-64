"""Compressed per-item/version facts with bounded, uncached iteration."""

from __future__ import annotations

import json
import sqlite3
import zlib
from collections.abc import Iterator, Mapping
from contextlib import closing
from pathlib import Path
from typing import Any

from unbake.project.config import Held


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
        return {"aliases": item["aliases"], "versions": versions}


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
        import os

        from unbake.typemap import storage

        self.connection.commit()
        self.connection.close()
        path = self.temporary.parent / ("facts-" + storage.file_digest(self.temporary) + ".sqlite")
        os.replace(self.temporary, path)
        return path

    def close(self) -> None:
        self.connection.close()
        self.temporary.unlink(missing_ok=True)
