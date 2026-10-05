"""The cycle's JSON-lines event stream (schema v1). Every event is validated before it is written."""

from __future__ import annotations

import json
import threading
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, TextIO

VERSION = 1

# event -> (required fields, optional fields)
SCHEMA: dict[str, tuple[frozenset[str], frozenset[str]]] = {
    name: (frozenset(required.split()), frozenset(optional.split()))
    for name, required, optional in (
        (
            "cycle.start",
            "project versions functions workers cores memory_total_bytes cache_root stop remote branch",
            "",
        ),
        ("fn.queued", "function bytes versions carryover best_percent", ""),
        ("fn.draft.start", "function", ""),
        ("fn.draft.done", "function ok file seconds", "diagnostic"),
        ("fn.edit", "function file sha256", ""),
        ("fn.compare.start", "function sha256", ""),
        ("fn.compare.done", "function sha256 per_version best_percent tries seconds", "diagnostic"),
        ("fn.exact", "function bytes sha256", ""),
        ("fn.landed", "function bytes versions seconds retried", ""),
        ("fn.land_failed", "function versions diagnostic returned_to_worker", ""),
        ("fn.committed", "function commit message", ""),
        ("fn.pushed", "commits remote branch ok", "error"),
        ("fn.held", "function key reason next", ""),
        ("fn.failed", "function key reason next", ""),
        ("worker.crash", "pid function task signal retried", ""),
        ("worker.memory", "pid function bytes cap_bytes", ""),
        ("step.run", "step trigger seconds", ""),
        ("types.refreshed", "steps seconds ok", "diagnostic"),
        ("fn.recheck", "function exact best_percent", "diagnostic"),
        ("cycle.end", "landed landed_bytes unpushed held carryovers exit next", ""),
    )
}


class SchemaError(ValueError):
    pass


def validate(event: str, fields: dict[str, Any]) -> None:
    if event not in SCHEMA:
        raise SchemaError(f"cycle.event: unknown event {event}")
    required, optional = SCHEMA[event]
    missing = required - fields.keys()
    extra = fields.keys() - required - optional
    if missing or extra:
        raise SchemaError(f"cycle.event {event}: missing {sorted(missing)}, unexpected {sorted(extra)}")


class Emitter:
    """Writes events to the stream (stdout) and hands each to listeners (the board, the state file)."""

    def __init__(self, stream: TextIO) -> None:
        self.stream = stream
        self.seq = 0
        self.lock = threading.Lock()
        self.listeners: list[Callable[[dict[str, Any]], None]] = []

    def emit(self, event: str, **fields: Any) -> dict[str, Any]:
        validate(event, fields)
        with self.lock:
            self.seq += 1
            record = {
                "v": VERSION,
                "seq": self.seq,
                "t": datetime.now(UTC).isoformat(),
                "event": event,
                **fields,
            }
            self.stream.write(json.dumps(record, sort_keys=True) + "\n")
            self.stream.flush()
        for listener in self.listeners:
            listener(record)
        return record
