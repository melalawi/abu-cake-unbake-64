"""File transport for facts decoding: a worker retains only one entry's JSON tree.

Both sides of the pool carry paths, not whole batches of encoded bytes or decoded tables. The private
directory belongs to the caller, including partial outputs left by a failed/retried worker. Compression
keeps the temporary transport small; pickle.dump/load stream without a second serialized tree in memory.
"""

from __future__ import annotations

import gzip
import io
import json
import pickle
from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast

Payload = bytes | Path
Batch = list[tuple[int, Payload]]
ROWS_PER_JOB = 256


def jobs(encoded: Mapping[int, bytes], count: int, directory: Path) -> list[Batch]:
    """Preserve inventory order and batch boundaries while staging read-only input files."""
    result: list[Batch] = []
    for start in range(0, count, ROWS_PER_JOB):
        batch: Batch = []
        for index in range(start, min(start + ROWS_PER_JOB, count)):
            path = directory / f"{index}.json"
            path.write_bytes(encoded[index])
            batch.append((index, path))
        result.append(batch)
    return result


def _one(data: Payload) -> Payload:
    # Keep this scope per entry: neither the result list nor the pickler retains earlier JSON trees.
    if isinstance(data, Path):
        with data.open("rb") as source:
            rows = json.load(source)
        target = data.with_suffix(".pickle.gz")
        with gzip.open(target, "wb", compresslevel=1) as output:
            pickle.dump(rows, output, protocol=5)
        return target
    # The bytes form also supports callers with a small in-memory payload.
    rows = json.loads(data)
    buffer = io.BytesIO()
    with gzip.GzipFile(fileobj=buffer, mode="wb", compresslevel=1) as output:
        pickle.dump(rows, output, protocol=5)
    return buffer.getvalue()


def decode(rows: Batch) -> Batch:
    return [(index, _one(data)) for index, data in rows]


def read(data: Payload) -> list[dict[str, Any]]:
    """Load one result in the parent; shared facts are still resolved by Store.decode there."""
    source = io.BytesIO(data) if isinstance(data, bytes) else data
    with gzip.open(source, "rb") as stream:
        return cast(list[dict[str, Any]], pickle.load(stream))
