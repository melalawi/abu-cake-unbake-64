"""JSON input boundary: duplicate object keys are evidence, never canonicalized away."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


class Pairs(list[tuple[str, Any]]):
    pass


def loads(content: str | bytes, context: str | Path) -> Any:
    def reject_constant(value: str) -> Any:
        raise ValueError(f"{context}: non-finite JSON number {value}")

    document = json.loads(content, object_pairs_hook=Pairs, parse_constant=reject_constant)

    def unpack(value: Any, location: str) -> Any:
        if isinstance(value, Pairs):
            result = {}
            for key, child in value:
                if key in result:
                    raise ValueError(f"{context}: duplicate JSON key {key!r} in {location}")
                result[key] = unpack(child, f"{location}.{key}")
            return result
        if isinstance(value, list):
            return [unpack(child, f"{location}[{index}]") for index, child in enumerate(value)]
        return value

    return unpack(document, "$")


def read(path: Path) -> Any:
    return loads(path.read_bytes(), path)
