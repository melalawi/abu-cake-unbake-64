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

    def _transform(self, value: Any, *, thaw: bool) -> Any:
        # Components share large user and evidence lists across their nodes. Keep
        # those references shared in the cached graph instead of copying them per node.
        known: dict[int, Any] = {}

        def visit(row: Any) -> Any:
            if not isinstance(row, (dict, list, tuple)):
                return row
            found = known.get(id(row))
            if found is not None:
                return found
            if isinstance(row, dict):
                if thaw and set(row) == {"$receipt"}:
                    result = self.rows[row["$receipt"]]
                elif not thaw and "sha256" in row and (index := self.indices.get(serialized(row))) is not None:
                    result = {"$receipt": index}
                else:
                    result = {name: visit(item) for name, item in row.items()}
            else:
                result = [visit(item) for item in row]
            known[id(row)] = result
            return result

        return visit(value)

    def freeze(self, value: Any) -> Any:
        return self._transform(value, thaw=False)

    def thaw(self, value: Any) -> Any:
        return self._transform(value, thaw=True)


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
