"""Reuse a solved graph when its facts stand, refreshing the evidence receipts."""

from __future__ import annotations

import hashlib
import io
import pickle
import re
from collections.abc import Callable
from functools import partial
from pathlib import Path
from typing import Any

from unbake import pool, tui
from unbake.cache import Cache, key, memo, serialized
from unbake.config import Held, Host, Project
from unbake.typemap import closure, facts

SCHEMA = 3


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

    def freeze(self, value: Any) -> tuple[int, bytes]:
        """Let the native pickle walker bind receipts, retaining shared graph objects without a Python copy."""
        buffer = io.BytesIO()
        with tui.task("Saving inferred type receipts"):
            _Writer(buffer, self.indices).dump(value)
        return len(self.rows), buffer.getvalue()

    def _transform(self, value: Any, objects: list[dict[str, Any]]) -> Any:
        known: dict[int, Any] = {}

        def visit(row: Any) -> Any:
            if not isinstance(row, (dict, list, tuple)):
                return row
            found = known.get(id(row))
            if found is not None:
                return found
            result: Any
            if isinstance(row, dict):
                if "sha256" in row and (index := self.indices.get(serialized(row))) is not None:
                    result = objects[index]
                else:
                    result = {name: visit(item) for name, item in row.items()}
            else:
                result = [visit(item) for item in row]
            known[id(row)] = result
            return result

        return visit(value)

    def thaw(self, frozen: tuple[int, bytes]) -> Any:
        """Load a fresh graph, binding only the receipt slots to this run's evidence."""
        count, content = frozen
        if count != len(self.rows):
            raise Held("solve", f"types.cache: {count} cached receipts for {len(self.rows)} current ones")
        return _Reader(io.BytesIO(content), [dict(row) for row in self.rows]).load()


class _Writer(pickle.Pickler):
    def __init__(self, stream: io.BytesIO, indices: dict[bytes, int]) -> None:
        super().__init__(stream, protocol=5)
        self.indices = indices

    def persistent_id(self, value: Any) -> Any:
        if isinstance(value, dict) and "sha256" in value:
            index = self.indices.get(serialized(value))
            if index is not None:
                return index
        return None


class _Reader(pickle.Unpickler):
    def __init__(self, stream: io.BytesIO, rows: list[dict[str, Any]]) -> None:
        super().__init__(stream)
        self.rows = rows

    def persistent_load(self, identity: Any) -> Any:
        if type(identity) is not int or not 0 <= identity < len(self.rows):
            raise Held("solve", "types.cache: invalid receipt slot")
        return self.rows[identity]


_SLOT = re.compile(rb'("\$receipt":)([0-9]+)')


def _slot(slots: list[int], match: re.Match[bytes]) -> bytes:
    return match[1] + str(slots[int(match[2])]).encode()


@pool.cpu
def _inputs_job(shared: Path, rows: list[tuple[int, str, dict[str, Any]]]) -> Any:
    cache = Cache(shared)
    found = []
    for index, content_key, seed in rows:

        def compute(seed: dict[str, Any] = seed) -> Any:
            receipts = Receipts()
            return serialized(receipts.inputs(seed)), receipts.rows

        normalized = closure.cached(cache, "types-input", [content_key], compute)
        found.append((index, normalized))
    return found


def _inputs(cache: Cache, seeds: list[dict[str, Any]], policy: Host) -> Any:
    """Only changed seed payloads need normalization jobs; local receipt slots are rebound in seed order."""
    result = {}
    pending = []
    for index, seed in enumerate(seeds):
        content_key = key(str(SCHEMA), serialized(seed))
        path = cache.get("types-input", key("types-input", content_key))
        if path is None:
            pending.append((index, content_key, seed))
        else:
            result[index] = memo("types.input", (str(cache.root), content_key), partial(closure.load, path), keep=32768)
    jobs = [pending[start : start + 128] for start in range(0, len(pending), 128)]
    for batch in pool.run(policy, _inputs_job, jobs, cache.root):
        result.update(batch)
    return [result[index] for index in range(len(seeds))]


def infer(
    project: Project,
    cache: Cache,
    parts: list[str],
    seeds: list[dict[str, Any]],
    compute: Callable[[], dict[str, Any]],
    *,
    output: facts.Store | None = None,
    policy: Host | None = None,
) -> tuple[dict[str, Any], str, str]:
    """All input facts and their order count; only evidence hashes are rebound on a hit. OUTPUT is the store the
    seeds were decoded with, which knows their shared values' digests."""
    output = facts.Store(project, cache) if output is None else output
    receipts = Receipts()
    digest = hashlib.sha256()
    with tui.task("Keying type inference facts", len(seeds)):
        rows: list[tuple[bytes, list[dict[str, Any]]]]
        if policy is None:
            rows = [(serialized(receipts.inputs(output.encode(seed))), []) for seed in seeds]
        else:
            rows = _inputs(cache, [output.encode(seed) for seed in seeds], policy)
        for data, local in rows:
            slots = []
            for row in local:
                identity = serialized(row)
                index = receipts.indices.get(identity)
                if index is None:
                    index = len(receipts.rows)
                    receipts.indices[identity] = index
                    receipts.rows.append(row)
                slots.append(index)
            if local:
                data = _SLOT.sub(partial(_slot, slots), data)
            digest.update(len(data).to_bytes(8, "big"))
            digest.update(data)
        content_key = key(str(SCHEMA), *parts, digest.digest())
    frozen = closure.cached(cache, "types-inferred", [content_key], lambda: receipts.freeze(compute()))
    return receipts.thaw(frozen), content_key, key(serialized(receipts.rows))
