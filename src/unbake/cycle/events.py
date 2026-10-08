"""The cycle's JSON-lines event stream (schema v2). Every event is validated before it is written."""

from __future__ import annotations

import json
import math
import threading
from collections import deque
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, TextIO

VERSION = 2

# event -> (required fields, optional fields)
SCHEMA: dict[str, tuple[frozenset[str], frozenset[str]]] = {
    name: (frozenset(required.split()), frozenset(optional.split()))
    for name, required, optional in (
        (
            "cycle.start",
            "project versions functions workers cores memory_total_bytes cache_root stop",
            "",
        ),
        ("fn.queued", "function bytes versions carryover best_percent", "subsystem_ref cohort_id"),
        ("fn.draft.start", "function", ""),
        ("fn.draft.done", "function ok file seconds", "diagnostic fault"),
        ("fn.edit", "function file sha256", ""),
        ("fn.compare.start", "function sha256", ""),
        ("fn.compare.done", "function sha256 per_version best_percent tries seconds", "diagnostic fault"),
        ("fn.search.start", "function method", ""),
        ("fn.search.done", "function method ok seconds", "diagnostic mutations measurements fault"),
        ("fn.creative", "function best_percent methods trouble", "subsystem_ref cohort_id"),
        ("fn.exact", "function bytes sha256", ""),
        ("fn.landed", "function bytes versions seconds", ""),
        ("fn.fuzzy_landed", "function bytes versions commit best_percent", ""),
        ("fn.land_failed", "function versions diagnostic returned_to_worker", "fault"),
        ("fn.committed", "function commit message", "proof"),
        ("cycle.committed", "commit message functions", ""),
        ("fn.held", "function key reason next", "fault"),
        ("fn.failed", "function key reason next", "fault"),
        ("worker.crash", "pid function task signal retried", ""),
        ("worker.memory", "pid function bytes cap_bytes", ""),
        (
            "step.run",
            "step trigger seconds",
            "wall_seconds cpu_seconds cpu_percent main_cpu_seconds tools_cpu_seconds pool_cpu_seconds "
            "main_rss_bytes worker_rss_bytes counts pool external_cpu_seconds external_cores changes findings",
        ),
        ("steps.held", "key reason", "fault"),
        ("fn.recheck", "function exact best_percent", "diagnostic fault"),
        ("cycle.end", "landed landed_bytes held carryovers exit next", "fuzzy"),
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

    strings = {
        "project",
        "cache_root",
        "stop",
        "function",
        "file",
        "sha256",
        "method",
        "diagnostic",
        "trouble",
        "subsystem_ref",
        "cohort_id",
        "commit",
        "message",
        "key",
        "reason",
        "step",
        "trigger",
        "task",
    }
    integers = {
        "bytes",
        "tries",
        "mutations",
        "landed_bytes",
        "memory_total_bytes",
        "cap_bytes",
        "main_rss_bytes",
        "worker_rss_bytes",
        "workers",
        "cores",
        "pid",
        "signal",
        "exit",
    }
    numbers = {
        "seconds",
        "wall_seconds",
        "cpu_seconds",
        "cpu_percent",
        "main_cpu_seconds",
        "tools_cpu_seconds",
        "pool_cpu_seconds",
        "external_cpu_seconds",
        "external_cores",
    }
    booleans = {"ok", "carryover", "exact", "retried", "returned_to_worker"}
    lists = {"versions", "functions", "landed", "held", "carryovers", "findings", "fuzzy"}

    def reject(name: str) -> None:
        raise SchemaError(f"cycle.event {event}.{name}: invalid type or range")

    def numeric(value: Any, *, percent: bool = False) -> bool:
        return type(value) in (int, float) and math.isfinite(value) and value >= 0 and (not percent or value <= 100)

    for name, value in fields.items():
        if name in strings:
            if type(value) is not str:
                reject(name)
        elif name in integers:
            if (
                type(value) is not int
                or value < 0
                or (name in {"workers", "cores", "pid", "cap_bytes", "memory_total_bytes"} and value == 0)
            ):
                reject(name)
            if name == "exit" and value > 255:
                reject(name)
        elif name in numbers:
            if not (value is None and name == "seconds") and not numeric(value):
                reject(name)
        elif name in booleans:
            if type(value) is not bool:
                reject(name)
        elif name in lists:
            if type(value) is not list or any(type(item) is not str for item in value):
                reject(name)
        elif name == "next":
            if value is not None and type(value) is not str:
                reject(name)
        elif name == "best_percent":
            if value is not None and not numeric(value, percent=True):
                reject(name)
        elif name == "measurements":
            if type(value) is not dict:
                reject(name)
            from unbake.work.score import Measurement

            for version, record in value.items():
                try:
                    measured = Measurement.read(record)
                    if measured.version != version:
                        reject(name)
                except (ValueError, KeyError, TypeError):
                    reject(name)
        elif name == "methods":
            if type(value) is not dict:
                reject(name)
            for method, outcome in value.items():
                if type(method) is not str or not method:
                    reject(name)
                if not numeric(outcome, percent=True) and not (
                    type(outcome) is str and outcome.startswith("skipped:") and outcome[8:].strip()
                ):
                    reject(name)
        elif name == "per_version":
            if type(value) is not dict:
                reject(name)
            for version, row in value.items():
                if (
                    type(version) is not str
                    or type(row) is not dict
                    or (
                        not {"percent", "exact", "first"} <= row.keys()
                        or row.keys() - {"percent", "exact", "first", "fault"}
                    )
                ):
                    reject(name)
                if "fault" in row and type(row["fault"]) is not dict:
                    reject(name)
                if (
                    (row["percent"] is not None and not numeric(row["percent"], percent=True))
                    or type(row["exact"]) is not bool
                    or type(row["first"]) is not str
                ):
                    reject(name)
        elif name in {"counts", "pool"}:
            if type(value) is not dict:
                reject(name)
            for label, row in value.items():
                if type(label) is not str or type(row) is not list or len(row) != 2:
                    reject(name)
                if name == "counts":
                    if any(type(item) is not int or item < 0 for item in row) or row[0] > row[1]:
                        reject(name)
                elif not numeric(row[0]) or type(row[1]) is not int or row[1] < 0:
                    reject(name)
        elif name in {"changes", "fault", "proof"}:
            if type(value) is not dict:
                reject(name)
            if name == "changes":
                for kind, row in value.items():
                    if type(kind) is not str or type(row) is not dict or set(row) != {"count", "first"}:
                        reject(name)
                    if (
                        type(row["count"]) is not int
                        or row["count"] < 0
                        or type(row["first"]) is not list
                        or any(type(item) is not str for item in row["first"])
                    ):
                        reject(name)
        else:
            reject(name)


class Emitter:
    """Writes events to the stream (stdout) and hands each to listeners (the board, the state file)."""

    def __init__(self, stream: TextIO) -> None:
        self.stream = stream
        self.seq = 0
        self.lock = threading.Lock()
        self.listeners: list[Callable[[dict[str, Any]], None]] = []
        self.pending: deque[tuple[dict[str, Any], tuple[Callable[[dict[str, Any]], None], ...]]] = deque()
        self.delivering = False

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
            self.pending.append((record, tuple(self.listeners)))
            deliver = not self.delivering
            if deliver:
                self.delivering = True
        if deliver:
            self._deliver()
        return record

    def _deliver(self) -> None:
        """One dispatcher; reentrant emits enqueue after the current listeners."""
        failure: BaseException | None = None
        while True:
            with self.lock:
                if not self.pending:
                    self.delivering = False
                    break
                record, listeners = self.pending.popleft()
            for listener in listeners:
                try:
                    listener(record)
                except BaseException as error:
                    if failure is None:
                        failure = error
        if failure is not None:
            raise failure
