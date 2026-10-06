"""Reuse a solved graph when its facts stand, refreshing the evidence receipts."""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from typing import Any

from unbake.cache import Cache, key, serialized
from unbake.config import Project
from unbake.typemap import closure, facts

SCHEMA = 1


class Receipts:
    """Keep evidence identities distinct while excluding their content stamps from type meaning."""

    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []
        self.indices: dict[bytes, int] = {}

    def inputs(self, value: Any) -> Any:
        if isinstance(value, dict):
            result = {}
            for name, row in sorted(value.items()):
                if name == "provenance" and isinstance(row, dict) and "sha256" in row:
                    identity = serialized(row)
                    index = self.indices.get(identity)
                    if index is None:
                        index = len(self.rows)
                        self.indices[identity] = index
                        self.rows.append(row)
                    result[name] = {"$receipt": index, **{k: v for k, v in row.items() if k != "sha256"}}
                else:
                    result[name] = self.inputs(row)
            return result
        if isinstance(value, (list, tuple)):
            return [self.inputs(row) for row in value]
        return value

    def freeze(self, value: Any) -> Any:
        if isinstance(value, dict):
            if "sha256" in value and (index := self.indices.get(serialized(value))) is not None:
                return {"$receipt": index}
            return {name: self.freeze(row) for name, row in value.items()}
        if isinstance(value, (list, tuple)):
            return [self.freeze(row) for row in value]
        return value

    def thaw(self, value: Any) -> Any:
        if isinstance(value, dict):
            if set(value) == {"$receipt"}:
                return self.rows[value["$receipt"]]
            return {name: self.thaw(row) for name, row in value.items()}
        if isinstance(value, list):
            return [self.thaw(row) for row in value]
        return value


def infer(
    project: Project,
    cache: Cache,
    parts: list[str],
    seeds: list[dict[str, Any]],
    compute: Callable[[], dict[str, Any]],
) -> tuple[dict[str, Any], str, str]:
    """All input facts and their order count; only evidence hashes are rebound on a hit."""
    output = facts.Store(project, cache)
    receipts = Receipts()
    digest = hashlib.sha256()
    for seed in seeds:
        data = serialized(receipts.inputs(output.encode(seed)))
        digest.update(len(data).to_bytes(8, "big"))
        digest.update(data)
    content_key = key(str(SCHEMA), *parts, digest.digest())
    frozen = closure.cached(cache, "types-inferred", [content_key], lambda: receipts.freeze(compute()))
    return receipts.thaw(frozen), content_key, key(serialized(receipts.rows))
