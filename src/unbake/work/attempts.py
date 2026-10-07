"""The schema2 immutable outcome ledger: one tracked history, one incremental command index."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import uuid
from collections.abc import Iterable, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, NoReturn

from unbake import atomic, strict_json
from unbake.config import Held, Project
from unbake.inputs import DependencySet, LogicalPath
from unbake.process import Action, Fault, RetryRule, capture
from unbake.process import named as cause_named
from unbake.work.score import Measurement

PATH = "attempts.jsonl"
FIELDS = frozenset(
    {
        "schema",
        "event_id",
        "operation_id",
        "project_id",
        "kind",
        "parents",
        "subject",
        "request",
        "dependencies",
        "result",
        "work",
        "observed_at",
    }
)


@dataclass(frozen=True)
class Operation:
    id: str
    project_id: str
    kind: str
    subject: str
    request: Mapping[str, Any]
    dependencies: DependencySet

    @classmethod
    def make(
        cls, project: Project, kind: str, subject: str, request: Mapping[str, Any], dependencies: DependencySet
    ) -> Operation:
        return cls(uuid.uuid4().hex, project.id, kind, subject, dict(request), dependencies)


@dataclass(frozen=True)
class Outcome:
    operation_id: str
    state: Literal["ok", "blocked", "committed"]
    value: Mapping[str, Any]
    fault: Fault | None
    work: Mapping[str, int]
    proof_ids: tuple[str, ...] = ()

    def document(self) -> dict[str, Any]:
        return {
            "operation_id": self.operation_id,
            "state": self.state,
            "value": dict(self.value),
            "fault": self.fault.document() if self.fault else None,
            "work": dict(self.work),
            "proof_ids": list(self.proof_ids),
        }


@dataclass(frozen=True)
class RetryDecision:
    allowed: bool
    changed: tuple[str, ...]
    reused: bool
    retryability: str


@dataclass(frozen=True)
class Event:
    schema: int
    event_id: str
    operation_id: str
    project_id: str
    kind: str
    parents: tuple[str, ...]
    subject: str
    request: Mapping[str, Any]
    dependencies: Mapping[str, Any]
    result: Mapping[str, Any]
    work: Mapping[str, int]
    observed_at: str

    def document(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class Publication:
    event_id: str
    function: str
    kind: Literal["exact", "fuzzy", "original"]
    source: LogicalPath | None
    source_sha256: str | None
    stored_source_sha256: str | None
    compiler: str | None
    versions: tuple[str, ...]
    measurements: Mapping[str, Measurement | None]
    receipt: Mapping[str, Any]
    dependency_set: DependencySet
    proof_ids: tuple[str, ...]
    committed_source_sha256: str | None
    origin: Literal["native", "history.imported"]
    available: bool
    unavailable_reason: str | None

    def similarity(self, version: str) -> float | None:
        """Current source-bound observation only; imported null never consults attempt best."""
        if not self.available or version not in self.versions:
            return None
        measured = self.measurements.get(version)
        if measured is not None:
            return measured.percent if measured.available else None
        value = self.receipt.get("versions", {}).get(version)
        return float(value) if value is not None else None

    def proof_reusable(self) -> bool:
        return (
            self.available
            and self.origin == "native"
            and bool(self.proof_ids)
            and not self.dependency_set.values.get("dependencies_unknown")
        )


@dataclass(frozen=True)
class Summary:
    bytes: int
    best: dict[str, float]
    exact: bool
    minutes: float
    attempts: int
    fuzzy: dict[str, Any] | None = None
    imported_bounds: tuple[int, int] | None = None

    @property
    def best_percent(self) -> float | None:
        return max(self.best.values(), default=None)

    def document(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def read(cls, value: Mapping[str, Any]) -> Summary:
        if set(value) != {"bytes", "best", "exact", "minutes", "attempts", "fuzzy", "imported_bounds"}:
            raise ValueError("history.imported: complete summary baseline required")
        if (
            any(type(value[k]) is not int or value[k] < 0 for k in ("bytes", "attempts"))
            or type(value["exact"]) is not bool
        ):
            raise ValueError("history.imported: invalid count or exact flag")
        if type(value["minutes"]) not in (int, float) or not math.isfinite(value["minutes"]) or value["minutes"] < 0:
            raise ValueError("history.imported: invalid effort baseline")
        if not isinstance(value["best"], dict) or any(
            not isinstance(k, str) or type(v) not in (int, float) or not math.isfinite(v) or not 0 <= v <= 100
            for k, v in value["best"].items()
        ):
            raise ValueError("history.imported: invalid score bounds")
        bounds = value["imported_bounds"]
        if bounds is not None and (
            not isinstance(bounds, (tuple, list))
            or len(bounds) != 2
            or any(type(v) is not int or v < 0 for v in bounds)
            or not bounds[0] <= value["attempts"] <= bounds[1]
        ):
            raise ValueError("history.imported: invalid attempt overlap bounds")
        return cls(
            value["bytes"],
            value["best"],
            value["exact"],
            value["minutes"],
            value["attempts"],
            value["fuzzy"],
            tuple(bounds) if bounds else None,
        )


@dataclass(frozen=True)
class Attempt:
    t: str
    function: str
    sha256: str
    bytes: int
    versions: dict[str, dict[str, Any]]
    best_percent: float | None
    exact: bool
    seconds: float
    compiler: str

    def document(self) -> dict[str, Any]:
        return asdict(self)


def encoded(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode()


def dependency_record(value: Mapping[str, Any]) -> DependencySet:
    from unbake.inputs import FilePin

    pins = tuple(
        FilePin(
            LogicalPath(row["path"]["root"], tuple(row["path"]["parts"])),
            row["state"],
            row["sha256"],
            LogicalPath(row["link_target"]["root"], tuple(row["link_target"]["parts"])) if row["link_target"] else None,
        )
        for row in value["files"]
    )
    return DependencySet(pins, value["values"], value["recipes"])


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds")


class Ledger:
    def __init__(self, project: Project) -> None:
        self.project = project
        self.path = project.root / PATH
        self.events: dict[str, dict[str, Any]] = {}
        self.order: list[str] = []
        self.inode: tuple[int, int] | None = None
        self.offset = 0
        self.prefix_reads = 0
        self.incremental_updates = 0

    def _refresh(self) -> None:
        if not self.path.exists():
            self.events.clear()
            self.order.clear()
            self.inode = None
            self.offset = 0
            if (self.project.root / "attempts.json").exists() or (self.project.build / "steps.json").exists():
                raise Held(
                    cause_named(
                        "ledger.migration",
                        "offline state migration required before ordinary commands",
                        owner="work.attempts",
                        stage="history",
                        action=Action("command", argv=("migrate-state", "--plan")),
                    )
                )
            return
        if self.path.is_symlink():
            raise Held(
                cause_named(
                    "ledger.symlink", "ledger must be a regular owned file", owner="work.attempts", stage="history"
                )
            )
        info = self.path.stat()
        inode = info.st_dev, info.st_ino
        if inode != self.inode or info.st_size < self.offset:
            self.events.clear()
            self.order.clear()
            self.offset = 0
            self.prefix_reads += 1
        if info.st_size == self.offset:
            return
        with self.path.open("rb") as stream:
            stream.seek(self.offset)
            data = stream.read()
        if data and not data.endswith(b"\n"):
            raise Held(
                cause_named(
                    "ledger.incomplete",
                    "truncated outcome record; preserve file and recover transaction",
                    owner="work.attempts",
                    stage="history",
                )
            )
        for line in data.splitlines():
            if not line:
                continue
            try:
                self._index(strict_json.loads(line, self.path))
            except (ValueError, TypeError, KeyError) as error:
                raise Held(
                    capture(
                        error,
                        cause=cause_named(
                            "ledger.corrupt", f"{self.path.name}: {error}", owner="work.attempts", stage="history"
                        ),
                    )
                ) from error
        self.inode, self.offset = inode, info.st_size

    def _index(self, event: dict[str, Any]) -> None:
        validate_event(event)
        if set(event) != FIELDS or event["schema"] != 2:
            raise Held(
                cause_named(
                    "ledger.schema",
                    "complete schema2 event required; use offline migration",
                    owner="work.attempts",
                    stage="history",
                )
            )
        if event["project_id"] != self.project.id:
            raise Held(
                cause_named(
                    "ledger.project", "event belongs to another project", owner="work.attempts", stage="history"
                )
            )
        identity = event["event_id"]
        prior = self.events.get(identity)
        if prior is not None:
            if encoded(prior) != encoded(event):
                raise Held(
                    cause_named(
                        "ledger.event_conflict",
                        f"conflicting immutable event {identity}",
                        owner="work.attempts",
                        stage="history",
                    )
                )
            return
        if any(parent not in self.events for parent in event["parents"]):
            raise Held(
                cause_named(
                    "ledger.parents", f"unavailable event parent for {identity}", owner="work.attempts", stage="history"
                )
            )
        self.events[identity] = event
        self.order.append(identity)
        self.incremental_updates += 1

    def append(self, event: Event) -> str:
        value = portable_tree(self.project, event.document())
        validate_event(value)
        lock = self.project.root / ".attempts.lock"
        with atomic.lock(lock):
            self._refresh()
            prior = self.events.get(event.event_id)
            if prior is not None:
                if encoded(prior) != encoded(value):
                    raise Held(
                        cause_named(
                            "ledger.event_conflict", "conflicting event id", owner="work.attempts", stage="history"
                        )
                    )
                return event.event_id
            if any(parent not in self.events for parent in value["parents"]):
                raise Held(
                    cause_named("ledger.parents", "event parent unavailable", owner="work.attempts", stage="history")
                )
            atomic.append_record(self.path, encoded(value) + b"\n", durable=True)
            self._index(value)
            info = self.path.stat()
            self.inode = info.st_dev, info.st_ino
            self.offset = info.st_size
        return event.event_id

    def record(self, operation: Operation, outcome: Outcome, *, parents: tuple[str, ...] = ()) -> str:
        if outcome.fault is not None:
            outcome = replace(outcome, fault=portable_fault(self.project, outcome.fault))
        outcome = replace(outcome, value=portable_value(self.project, outcome.value))
        return self.append(
            Event(
                2,
                uuid.uuid4().hex,
                operation.id,
                operation.project_id,
                operation.kind,
                parents,
                operation.subject,
                dict(operation.request),
                operation.dependencies.document(),
                outcome.document(),
                dict(outcome.work),
                now(),
            )
        )

    def outcome(self, operation: Operation) -> Outcome | None:
        self._refresh()
        for identity in reversed(self.order):
            row = self.events[identity]
            if (row["kind"], row["subject"], row["request"], row["dependencies"]) != (
                operation.kind,
                operation.subject,
                dict(operation.request),
                operation.dependencies.document(),
            ):
                continue
            data = row["result"]
            if row["kind"] == "history.imported":
                continue
            return Outcome(
                data["operation_id"],
                data["state"],
                data["value"],
                Fault.read(data["fault"]) if data["fault"] else None,
                data["work"],
                tuple(data["proof_ids"]),
            )
        return None

    def retry(self, operation: Operation, current: DependencySet) -> RetryDecision:
        self._refresh()
        candidate = next(
            (
                self.events[i]
                for i in reversed(self.order)
                if self.events[i]["kind"] == operation.kind
                and self.events[i]["subject"] == operation.subject
                and self.events[i]["result"]["state"] == "blocked"
            ),
            None,
        )
        if candidate is None:
            return RetryDecision(True, (), False, "unblocked")
        fault = Fault.read(candidate["result"]["fault"])
        if fault.cause.retryability == "unknown":
            return RetryDecision(True, (), False, "unknown")
        changes = dependency_changes(fault.cause.dependency_set, current)
        watched = fault.cause.retry.watch
        relevant = tuple(name for name in changes if not watched or name in watched)
        allowed = bool(relevant) and fault.cause.retry.kind != "never"
        return RetryDecision(allowed, relevant, not allowed, fault.cause.retryability)

    def blocked(self, *, operation: str | None = None) -> tuple[Outcome, ...]:
        self._refresh()
        latest = {}
        for identity in self.order:
            row = self.events[identity]
            if operation is None or row["kind"] == operation:
                latest[row["kind"], row["subject"]] = row
        return tuple(
            Outcome(
                row["operation_id"],
                "blocked",
                row["result"]["value"],
                Fault.read(row["result"]["fault"]),
                row["work"],
                tuple(row["result"]["proof_ids"]),
            )
            for row in sorted(latest.values(), key=lambda r: (r["kind"], r["subject"]))
            if row["result"]["state"] == "blocked"
        )

    def latest(self, kind: str, subject: str) -> dict[str, Any] | None:
        self._refresh()
        return next(
            (
                self.events[i]
                for i in reversed(self.order)
                if self.events[i]["kind"] == kind and self.events[i]["subject"] == subject
            ),
            None,
        )

    def note(
        self,
        kind: str,
        subject: str,
        value: Mapping[str, Any],
        *,
        dependencies: DependencySet,
        state: Literal["ok", "blocked", "committed"] = "ok",
        fault: Fault | None = None,
    ) -> str:
        operation = Operation.make(self.project, kind, subject, {}, dependencies)
        previous = self.latest(kind, subject)
        return self.record(
            operation,
            Outcome(operation.id, state, value, fault, {}),
            parents=(previous["event_id"],) if previous else (),
        )

    def step(self, name: str) -> dict[str, Any] | None:
        event = self.latest("step", name)
        return (
            dict(event["result"]["value"])
            if event and event["result"]["state"] == "ok" and "key" in event["result"]["value"]
            else None
        )

    def record_step(self, name: str, content_key: str, outputs: Mapping[str, str]) -> str:
        value = {"key": content_key, "outputs": dict(outputs)}
        previous = self.latest("step", name)
        if previous and previous["result"]["state"] == "ok" and previous["result"]["value"] == value:
            return str(previous["event_id"])
        return self.note("step", name, value, dependencies=DependencySet((), {"input_key": content_key}, {}))

    def rename(self, renamed: Mapping[str, str]) -> None:
        for old, new in sorted(renamed.items()):
            self.note(
                "source.rename",
                old,
                {"old": old, "new": new},
                dependencies=DependencySet((), {"dependencies_unknown": True}, {}),
            )

    def redrafts(self) -> dict[str, Any]:
        self._refresh()
        marks: dict[str, Any] = {}
        for identity in self.order:
            event = self.events[identity]
            if event["kind"] == "draft.required":
                value = event["result"]["value"]
                if value["mark"] is None:
                    marks.pop(event["subject"], None)
                else:
                    marks[event["subject"]] = value["mark"]
        return marks

    def mark_drafts(self, marks: Mapping[str, Any]) -> None:
        previous = self.redrafts()
        for function in sorted(previous.keys() | marks.keys()):
            if previous.get(function) != marks.get(function):
                self.note(
                    "draft.required",
                    function,
                    {"mark": marks.get(function)},
                    dependencies=DependencySet(
                        (), {"type_db_sha256": (marks.get(function) or {}).get("type_db_sha256")}, {}
                    ),
                )

    def history(self, function: str) -> list[Attempt]:
        self._refresh()
        return [
            Attempt(**row["result"]["value"]["attempt"])
            for identity in self.order
            for row in [self.events[identity]]
            if row["subject"] == function and row["kind"] == "compare" and "attempt" in row["result"]["value"]
        ]

    def compare(self, attempt: Attempt, dependencies: DependencySet) -> str:
        operation = Operation.make(
            self.project, "compare", attempt.function, {"source_sha256": attempt.sha256}, dependencies
        )
        return self.record(
            operation,
            Outcome(
                operation.id,
                "ok",
                {"attempt": attempt.document()},
                None,
                {},
            ),
        )

    def summaries(self) -> dict[str, Summary]:
        self._refresh()
        table = {}
        for identity in self.order:
            row = self.events[identity]
            subject = row["subject"]
            data = row["result"]["value"]
            if row["kind"] == "history.imported":
                table[subject] = Summary.read(data["summary"])
            elif row["kind"] == "compare" and "attempt" in data:
                attempt = Attempt(**data["attempt"])
                previous = table.get(subject, Summary(0, {}, False, 0.0, 0))
                best = dict(previous.best)
                for version, value in attempt.versions.items():
                    percent = value.get("percent")
                    if percent is not None and not value.get("fault"):
                        best[version] = max(best.get(version, 0.0), float(percent))
                table[subject] = Summary(
                    attempt.bytes,
                    best,
                    previous.exact or attempt.exact,
                    previous.minutes + attempt.seconds / 60,
                    previous.attempts + 1,
                    previous.fuzzy,
                    previous.imported_bounds,
                )
            elif row["kind"] == "publication.fuzzy":
                previous = table.get(subject, Summary(0, {}, False, 0.0, 0))
                table[subject] = Summary(
                    previous.bytes,
                    previous.best,
                    previous.exact,
                    previous.minutes,
                    previous.attempts,
                    data["publication"]["receipt"],
                    previous.imported_bounds,
                )
            elif row["kind"] in ("publication.exact", "publication.original"):
                previous = table.get(subject, Summary(0, {}, False, 0.0, 0))
                table[subject] = replace(previous, fuzzy=None, exact=True)
            elif row["kind"] == "source.rename":
                prior = table.pop(data["old"], None)
                if prior is not None:
                    table[data["new"]] = prior
        return table

    def publications(self) -> Mapping[str, Publication]:
        self._refresh()
        current: dict[str, Publication] = {}
        for identity in self.order:
            event = self.events[identity]
            if event["kind"] == "source.rename":
                data = event["result"]["value"]
                old = current.pop(data["old"], None)
                if old is not None:
                    current[data["new"]] = replace(
                        old,
                        function=data["new"],
                        available=False,
                        unavailable_reason="renamed source requires current binding and proof",
                    )
                continue
            if event["kind"] not in ("publication.exact", "publication.fuzzy", "publication.original"):
                continue
            if event["result"]["state"] != "committed":
                continue
            data = event["result"]["value"]["publication"]
            source = data["source"]
            dependencies = dependency_record(event["dependencies"])
            current[event["subject"]] = Publication(
                identity,
                event["subject"],
                data["kind"],
                LogicalPath(source["root"], tuple(source["parts"])) if source else None,
                data["source_sha256"],
                data["stored_source_sha256"],
                data["compiler"],
                tuple(data["versions"]),
                {
                    v: Measurement.read(value) if value is not None else None
                    for v, value in data["measurements"].items()
                },
                data["receipt"],
                dependencies,
                tuple(event["result"]["proof_ids"]),
                data["committed_source_sha256"],
                data["origin"],
                data["available"],
                data["unavailable_reason"],
            )
        return current

    def publication(
        self,
        function: str,
        *,
        version: str | None = None,
        source_sha256: str | None = None,
        compiler: str | None = None,
    ) -> Publication | None:
        record = self.publications().get(function)
        if record is None:
            return None
        mismatch = []
        if version is not None and version not in record.versions:
            mismatch.append("holding version")
        if source_sha256 is not None and source_sha256 != record.source_sha256:
            mismatch.append("source hash")
        if compiler is not None and compiler != record.compiler:
            mismatch.append("compiler")
        return (
            replace(record, available=False, unavailable_reason="changed " + ", ".join(mismatch))
            if mismatch
            else record
        )

    def fuzzy(self, function: str) -> dict[str, Any] | None:
        record = self.publication(function)
        return dict(record.receipt) if record is not None and record.available and record.kind == "fuzzy" else None

    def retained_sources(self) -> dict[str, dict[str, Any]]:
        self._refresh()
        retained = {}
        for identity in self.order:
            event = self.events[identity]
            if event["kind"] == "source.retained":
                retained[event["subject"]] = dict(event["result"]["value"]["retained"])
            elif event["kind"].startswith("publication.") and event["result"]["state"] == "committed":
                retained.pop(event["subject"], None)
        return retained

    def fuzzy_sources(self) -> dict[str, dict[str, Any]]:
        result = {
            name: {
                "source_sha256": row["source_sha256"],
                "compiler": row["compiler"],
                "score": None,
                "versions": {v: None for v in row["versions"]},
            }
            for name, row in self.retained_sources().items()
        }
        result.update(
            {
                name: dict(record.receipt)
                for name, record in self.publications().items()
                if record.available and record.kind == "fuzzy"
            }
        )
        return result

    def record_publication(
        self,
        function: str,
        receipt: Mapping[str, Any] | None,
        source: str,
        versions: Iterable[str],
        compiler: str,
        dependencies: DependencySet,
        *,
        committed: bool = True,
        kind: Literal["exact", "fuzzy", "original"] | None = None,
    ) -> str:
        from unbake.inputs import bytes_digest

        kind = kind or ("fuzzy" if receipt is not None else "exact")
        digest = bytes_digest(source.encode(), algorithm="sha256")
        publication = {
            "kind": kind,
            "source": {"root": "project", "parts": ["src", function + (".s" if kind == "original" else ".c")]},
            "source_sha256": digest,
            "stored_source_sha256": digest,
            "compiler": compiler,
            "versions": list(versions),
            "measurements": {v: None for v in versions},
            "receipt": dict(receipt or {}),
            "committed_source_sha256": digest if committed else None,
            "origin": "native",
            "available": True,
            "unavailable_reason": None,
        }
        if committed:
            identity = self.note(
                "publication." + kind,
                function,
                {"publication": publication},
                dependencies=dependencies,
                state="committed",
            )
        else:
            operation = Operation.make(self.project, "publication.prepared", function, {}, dependencies)
            prior = self.publications().get(function)
            previous = self.latest("publication.prepared", function)
            parents = tuple(
                dict.fromkeys((*((prior.event_id,) if prior else ()), *((previous["event_id"],) if previous else ())))
            )
            identity = self.record(
                operation, Outcome(operation.id, "ok", {"publication": publication}, None, {}), parents=parents
            )
        if not committed:
            from unbake import journal

            journal.prepared(identity)
        return identity

    def assert_portable(self) -> None:
        self._refresh()
        for identity in self.order:
            row = self.events[identity]
            if portable_tree(self.project, row) != row:
                raise Held(
                    cause_named(
                        "ledger.portability",
                        "public history contains host paths; review preserved portable-history migration",
                        owner="work.attempts",
                        stage="publish",
                        subject=identity,
                        action=Action("command", argv=("migrate-state", "--plan")),
                    )
                )

    def batch(self, batch_id: str) -> tuple[dict[str, Any], ...]:
        self._refresh()
        return tuple(self.events[i] for i in self.order if self.events[i]["operation_id"] == batch_id)

    @staticmethod
    def merge(base: bytes, ours: bytes, theirs: bytes) -> bytes:
        events: dict[str, dict[str, Any]] = {}
        for content in (base, ours, theirs):
            for line in content.splitlines():
                row = strict_json.loads(line, "ledger merge")
                validate_event(row)
                identity = row["event_id"]
                if row.get("schema") != 2 or set(row) != FIELDS:
                    raise ValueError("ledger.schema: complete schema2 event required")
                if identity in events and encoded(events[identity]) != encoded(row):
                    raise ValueError("ledger.event_conflict: conflicting immutable id")
                events[identity] = row
        aliases: dict[str, str] = {}
        for identity, row in events.items():
            origin = row["request"].get("representation_origin")
            if origin is not None:
                old = origin["event_id"]
                prior = aliases.get(old)
                if prior is not None and prior != identity:
                    raise ValueError("ledger.representation_conflict")
                if old in events and hashlib.sha256(encoded(events[old])).hexdigest() != origin["payload_sha256"]:
                    raise ValueError("ledger.representation_origin")
                aliases[old] = identity
        for old in aliases:
            events.pop(old, None)
        # Representation transitions remap descendants through the same immutable event rule.
        while True:
            changed = False
            for identity, row in list(events.items()):
                parents = [aliases.get(parent, parent) for parent in row["parents"]]
                if parents == row["parents"]:
                    continue
                original = encoded(row)
                new = uuid.uuid5(
                    uuid.NAMESPACE_URL, "current-ledger:" + identity + ":" + hashlib.sha256(original).hexdigest()
                ).hex
                rewritten = strict_json.loads(original, "ledger representation merge")
                rewritten["event_id"], rewritten["parents"] = new, parents
                rewritten["request"]["representation_origin"] = {
                    "event_id": identity,
                    "payload_sha256": hashlib.sha256(original).hexdigest(),
                }
                validate_event(rewritten)
                if new in events and encoded(events[new]) != encoded(rewritten):
                    raise ValueError("ledger.representation_conflict")
                events.pop(identity)
                events[new] = rewritten
                aliases[identity] = new
                changed = True
            if not changed:
                break
        remaining = set(events)
        written: set[str] = set()
        order = []
        while remaining:
            ready = sorted(i for i in remaining if set(events[i]["parents"]) <= written)
            if not ready:
                raise ValueError("ledger.parents: unavailable or cyclic parents")
            for identity in ready:
                order.append(events[identity])
                written.add(identity)
                remaining.remove(identity)
        return b"".join(encoded(row) + b"\n" for row in order)


_current: ContextVar[Ledger | None] = ContextVar("unbake_ledger", default=None)


@contextmanager
def command_ledger(project: Project) -> Any:
    current = _current.get()
    token = _current.set(current if current is not None and current.project.root == project.root else Ledger(project))
    try:
        yield _current.get()
    finally:
        _current.reset(token)


def ledger(project: Project) -> Ledger:
    current = _current.get()
    return current if current is not None and current.project.root == project.root else Ledger(project)


FUZZY_PREFIX = "#ifdef NON_MATCHING\n"
FUZZY_SUFFIX = "#endif /* NON_MATCHING */\n"


def guarded(text: str) -> str:
    """The retained draft is opt-in C; default ROM ownership stays with its assembly rows."""
    return FUZZY_PREFIX + text.rstrip() + "\n" + FUZZY_SUFFIX


def guard_present(text: str) -> bool:
    """Comments and literals cannot create a NON_MATCHING directive."""
    masked = re.sub(
        r"/\*.*?\*/|//[^\n]*|\"(?:\\[\s\S]|[^\"\\])*\"|'(?:\\[\s\S]|[^'\\])*'",
        lambda match: re.sub(r"[^\n]", " ", match[0]),
        text,
        flags=re.S,
    )
    return bool(re.search(r"^[ \t]*#[ \t]*(?:ifdef|ifndef|if|elif)\b[^\n]*\bNON_MATCHING\b", masked, re.M))


def guard_body(text: str, *, subject: str) -> str:
    """One balanced whole-file NON_MATCHING opt-in guard; closing comments are not authority."""
    masked = re.sub(
        r"/\*.*?\*/|//[^\n]*|\"(?:\\[\s\S]|[^\"\\])*\"|'(?:\\[\s\S]|[^'\\])*'",
        lambda match: re.sub(r"[^\n]", " ", match[0]),
        text,
        flags=re.S,
    )
    directives = list(re.finditer(r"^[ \t]*#[ \t]*(ifdef|ifndef|if|elif|else|endif)\b([^\n]*)(?:\n|$)", masked, re.M))
    valid = False
    if directives and not masked[: directives[0].start()].strip():
        first = directives[0]
        expression = first[2].strip()
        opened = (first[1] == "ifdef" and expression == "NON_MATCHING") or (
            first[1] == "if"
            and re.fullmatch(r"defined[ \t]*(?:\([ \t]*NON_MATCHING[ \t]*\)|[ \t]+NON_MATCHING)", expression)
        )
        branches: list[bool] = []
        valid = bool(opened)
        last = None
        for index, directive in enumerate(directives):
            kind, argument = directive[1], directive[2].strip()
            if kind in ("if", "ifdef", "ifndef"):
                valid = valid and bool(argument)
                branches.append(False)
            elif kind == "endif":
                if not branches or argument:
                    valid = False
                    break
                branches.pop()
                if not branches:
                    last = directive
                    valid = valid and index == len(directives) - 1 and not masked[directive.end() :].strip()
            else:
                if len(branches) <= 1 or branches[-1]:
                    valid = False
                    break
                if kind == "else":
                    valid = valid and not argument
                    branches[-1] = True
                else:
                    valid = valid and bool(argument)
        valid = valid and not branches and last is not None
        if valid and last is not None:
            return text[first.end() : last.start()]
    raise Held(
        cause_named(
            "fuzzy.source.guard",
            f"{subject}: expected a balanced whole-file NON_MATCHING guard without an outer else branch",
            owner="work.attempts",
            stage="source",
            subject=subject,
        )
    )


def unguarded(text: str) -> str:
    return guard_body(text, subject="retained source")


# Draft history moves with a rename: text files carry the new name; compiled objects are rebuilt, not carried.
_TEXT = frozenset({".c", ".h", ".txt", ".s", ".md"})


@dataclass(frozen=True)
class Carry:
    old: Path
    staged: Path
    new: Path


def stage_renames(project: Project, renamed: dict[str, str]) -> list[Carry]:
    """Each renamed function's work directory, copied under its new name beside build/work (nothing live moves
    yet): paths and text name the new function. Refused when the new name already has a work directory."""
    carries = []
    try:
        for old, new in sorted(renamed.items()):
            source = project.work / old
            if not source.is_dir():
                continue
            target = project.work / new
            if target.exists():
                raise Held(
                    cause_named(
                        "attempts.rename",
                        f"attempts.rename: {target} exists; {source} cannot carry its history there",
                        owner="work.attempts",
                        stage="work",
                    )
                )
            staged = project.work / f".{new}.staged"
            shutil.rmtree(staged, ignore_errors=True)
            carries.append(Carry(source, staged, target))
            word = re.compile(rf"\b{re.escape(old)}\b")
            for path in sorted(source.rglob("*")):
                if not path.is_file() or path.suffix not in _TEXT:
                    continue
                relative = Path(*(part.replace(old, new) for part in path.relative_to(source).parts))
                atomic.text(staged / relative, word.sub(new, path.read_text(errors="replace")))
            staged.mkdir(parents=True, exist_ok=True)
    except BaseException:
        discard(carries)
        raise
    return carries


def install(carries: list[Carry]) -> None:
    """Swap every staged directory in under its new name and drop the old one."""
    for carry in carries:
        os.rename(carry.staged, carry.new)
        shutil.rmtree(carry.old)


def discard(carries: list[Carry]) -> None:
    for carry in carries:
        shutil.rmtree(carry.staged, ignore_errors=True)


def dependency_changes(before: DependencySet, after: DependencySet) -> tuple[str, ...]:
    def flatten(value: DependencySet) -> dict[str, Any]:
        return {
            **{pin.path.name: asdict(pin) for pin in value.files},
            **{"value:" + k: v for k, v in value.values.items()},
            **{"recipe:" + k: v for k, v in value.recipes.items()},
        }

    old, new = flatten(before), flatten(after)
    return tuple(sorted(k for k in old.keys() | new.keys() if k not in old or k not in new or old[k] != new[k]))


def validate_event(event: Mapping[str, Any]) -> None:
    def refuse(reason: str) -> None:
        raise Held(cause_named("ledger.corrupt", reason, owner="work.attempts", stage="history"))

    if (
        not isinstance(event, dict)
        or set(event) != FIELDS
        or type(event.get("schema")) is not int
        or event["schema"] != 2
    ):
        refuse("complete schema2 event required; preserve history and use offline migration")
    for name in ("event_id", "operation_id", "project_id", "kind", "subject", "observed_at"):
        if not isinstance(event[name], str) or not event[name]:
            refuse("invalid ledger " + name)
    if not re.fullmatch(r"[0-9a-f]{32}", event["event_id"]) or not re.fullmatch(r"[0-9a-f]{32}", event["operation_id"]):
        refuse("invalid event/operation identity")
    if not isinstance(event["parents"], (tuple, list)) or any(
        not isinstance(p, str) or not re.fullmatch(r"[0-9a-f]{32}", p) for p in event["parents"]
    ):
        refuse("invalid parent identities")
    for name in ("request", "dependencies", "result", "work"):
        if not isinstance(event[name], dict):
            refuse("invalid ledger " + name)
    if any(not isinstance(k, str) or type(v) is not int or v < 0 for k, v in event["work"].items()):
        refuse("work requires nonnegative integer counts")
    try:
        dependencies = dependency_record(event["dependencies"])
        result = event["result"]
        if (
            set(result) != {"operation_id", "state", "value", "fault", "work", "proof_ids"}
            or result["operation_id"] != event["operation_id"]
            or result["state"] not in ("ok", "blocked", "committed")
            or not isinstance(result["value"], dict)
            or result["work"] != event["work"]
        ):
            refuse("invalid terminal outcome")
        if result["fault"] is not None:
            Fault.read(result["fault"])
        if result["state"] == "blocked" and result["fault"] is None:
            refuse("blocked outcome requires owning fault")
        if not isinstance(result["proof_ids"], (tuple, list)) or any(
            not isinstance(v, str) or not re.fullmatch(r"[0-9a-f]{64}", v) for v in result["proof_ids"]
        ):
            refuse("invalid proof identities")
        if event["kind"] in ("publication.exact", "publication.fuzzy", "publication.original", "publication.prepared"):
            validate_publication(result["value"]["publication"])
        if event["kind"] == "source.retained":
            row = result["value"]["retained"]
            if (
                result["state"] != "ok"
                or result["proof_ids"]
                or set(row) != {"source_sha256", "compiler", "versions", "verification"}
                or row["verification"] != "unverified"
                or not isinstance(row["source_sha256"], str)
                or not re.fullmatch(r"[0-9a-f]{64}", row["source_sha256"])
                or not isinstance(row["compiler"], str)
                or not row["compiler"]
                or not isinstance(row["versions"], list)
                or not row["versions"]
                or any(not isinstance(v, str) or not v for v in row["versions"])
                or len(set(row["versions"])) != len(row["versions"])
            ):
                refuse("invalid unverified retained source identity")
        if event["kind"] == "history.imported":
            Summary.read(result["value"]["summary"])
        encoded(dependencies.document())
    except (KeyError, TypeError, ValueError, AttributeError) as error:
        refuse(f"invalid ledger outcome: {error}")


class RetryScope:
    """Coordinator boundary: record first refusal once, retry only watched dependencies."""

    def __init__(
        self, project: Project, kind: str, subject: str, request: Mapping[str, Any], dependencies: DependencySet
    ) -> None:
        self.ledger = ledger(project)
        prior = self.ledger.latest(kind, subject)
        self.operation = Operation(
            prior["operation_id"] if prior and prior["request"] == dict(request) else uuid.uuid4().hex,
            project.id,
            kind,
            subject,
            request,
            dependencies,
        )
        self.value: dict[str, Any] = {}
        self.fault: Fault | None = None
        self.scope: Any = None
        self.before: dict[str, tuple[int, int]] = {}

    def __enter__(self) -> RetryScope:
        from unbake.process import cause_scope

        decision = self.ledger.retry(self.operation, self.operation.dependencies)
        if not decision.allowed:
            previous = self.ledger.latest(self.operation.kind, self.operation.subject)
            assert previous is not None
            raise Held(
                Fault.read(previous["result"]["fault"]),
                data={
                    **previous["result"]["value"],
                    "reused": True,
                    "retry_allowed": False,
                    "changed_dependencies": [],
                    "work": {"retries": 0, "native_calls": 0, "worker_calls": 0},
                },
            )
        self.scope = cause_scope(
            self.operation.subject,
            self.operation.dependencies,
            complete=not self.operation.dependencies.values.get("dependencies_unknown", False),
        )
        self.scope.__enter__()
        from unbake import effort

        self.before = effort.counted()
        return self

    def __exit__(self, kind: Any, error: Any, tb: Any) -> Literal[False]:
        self.scope.__exit__(kind, error, tb)
        if error is not None and not isinstance(error, Exception):
            return False
        if error is not None and not isinstance(error, Held):
            from unbake.process import capture

            error = Held(
                capture(
                    error,
                    cause=cause_named(
                        "operation.unexpected",
                        f"{type(error).__name__}: {error}",
                        owner=self.operation.kind,
                        stage=self.operation.kind,
                        subject=self.operation.subject,
                        dependencies=DependencySet((), {"dependencies_unknown": True}, {}),
                    ),
                )
            )
        if error is not None and error.fault.cause.owner == "pool" and error.key == "worker.memory":
            dependencies = self.operation.dependencies
            watch = (
                *tuple(p.path.name for p in dependencies.files),
                "value:memory_worker_bytes",
                *tuple("recipe:" + k for k in dependencies.recipes),
            )
            error.fault = replace(
                error.fault,
                cause=replace(error.fault.cause, dependency_set=dependencies, retry=RetryRule("dependencies", watch)),
            )
        if (
            error is not None
            and error.fault.cause.subject == "headers"
            and error.fault.cause.owner in ("typemap.declarations", "cdecl")
        ):
            dependencies = error.fault.cause.dependency_set
            pins = tuple(
                pin for pin in dependencies.files if not (pin.path.root == "project" and pin.path.parts[:1] == ("src",))
            )
            bounded = replace(dependencies, files=pins)
            watch = (
                *tuple(p.path.name for p in pins),
                *tuple("value:" + k for k in bounded.values),
                *tuple("recipe:" + k for k in bounded.recipes),
            )
            error.fault = replace(
                error.fault,
                cause=replace(error.fault.cause, dependency_set=bounded, retry=RetryRule("dependencies", watch)),
            )
        from unbake import effort

        work = {
            name: counts[0] - self.before.get(name, (0, 0))[0]
            for name, counts in effort.counted().items()
            if counts[0] != self.before.get(name, (0, 0))[0]
        }
        outcome = Outcome(
            self.operation.id,
            "blocked" if error or self.fault else "ok",
            error.data if error else self.value,
            error.fault if error else self.fault,
            work,
        )
        previous = self.ledger.latest(self.operation.kind, self.operation.subject)
        self.ledger.record(self.operation, outcome, parents=(previous["event_id"],) if previous else ())
        return False


def portable_fault(project: Project, fault: Fault) -> Fault:
    raw = encoded(fault.document())
    artifact = hashlib.sha256(raw).hexdigest()
    destination = project.work / "faults" / (artifact + ".json")
    if not destination.is_file():
        atomic.write(destination, raw)

    document = portable_tree(project, fault.document())
    document["cause"]["evidence"]["native_artifact"] = artifact
    return Fault.read(document)


def retry_pending(project: Project, kind: str, subject: str, current: DependencySet) -> bool:
    history = ledger(project)
    previous = history.latest(kind, subject)
    if previous is None:
        return True
    if previous["result"]["state"] != "blocked":
        old = dependency_record(previous["dependencies"])
        return bool(dependency_changes(old, current)) or bool(old.values.get("dependencies_unknown", False))
    operation = Operation(previous["operation_id"], project.id, kind, subject, previous["request"], current)
    return history.retry(operation, current).allowed


def validate_publication(value: Mapping[str, Any]) -> None:
    required = {
        "kind",
        "source",
        "source_sha256",
        "stored_source_sha256",
        "compiler",
        "versions",
        "measurements",
        "receipt",
        "committed_source_sha256",
        "origin",
        "available",
        "unavailable_reason",
    }
    if (
        set(value) != required
        or value["kind"] not in ("exact", "fuzzy", "original")
        or value["origin"] not in ("native", "history.imported")
        or type(value["available"]) is not bool
    ):
        raise ValueError("publication.schema: explicit current-publication record required")
    for field in ("source_sha256", "stored_source_sha256", "committed_source_sha256"):
        digest = value[field]
        if digest is not None and (not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest)):
            raise ValueError("publication.hash: invalid " + field)
    if (
        not isinstance(value["versions"], (tuple, list))
        or not value["versions"]
        or any(not isinstance(v, str) or not v for v in value["versions"])
        or len(set(value["versions"])) != len(value["versions"])
    ):
        raise ValueError("publication.versions: explicit distinct versions required")
    if set(value["measurements"]) != set(value["versions"]):
        raise ValueError("publication.measurements: every version required")
    if value["source"] is not None:
        source = LogicalPath(value["source"]["root"], tuple(value["source"]["parts"]))
        if source.root != "project" or not source.parts or source.parts[0] != "src":
            raise ValueError("publication.source: owned source required")
    for version, measured in value["measurements"].items():
        if measured is not None and Measurement.read(measured).version != version:
            raise ValueError("publication.measurement: version mismatch")
    if value["kind"] == "fuzzy":
        receipt = value["receipt"]
        if (
            set(receipt) != {"source_sha256", "compiler", "score", "versions"}
            or receipt["source_sha256"] != value["stored_source_sha256"]
            or receipt["compiler"] != value["compiler"]
            or set(receipt["versions"]) != set(value["versions"])
        ):
            raise ValueError("publication.receipt: exact source/compiler/version binding required")
        for percent in (receipt["score"], *receipt["versions"].values()):
            if percent is not None and (
                type(percent) not in (float, int) or not math.isfinite(percent) or not 0 <= percent <= 100
            ):
                raise ValueError("publication.score: unknown is null, measured is finite percent")


_producer: ContextVar[Operation | None] = ContextVar("unbake_producer", default=None)


@contextmanager
def producing(operation: Operation) -> Any:
    """Workers transport outcomes; only their coordinator appends authoritative history."""
    from unbake.process import cause_scope

    token = _producer.set(operation)
    try:
        with cause_scope(
            operation.subject,
            operation.dependencies,
            complete=not operation.dependencies.values.get("dependencies_unknown", False),
        ):
            yield
    finally:
        _producer.reset(token)


def producer_operation() -> Operation | None:
    return _producer.get()


def canonical_search(project: Project, value: Any) -> Any:
    """One offline decoder for the exact former Search dataclass representation.

    Parse syntax with an explicit node allowlist; never evaluate Python or
    infer missing search fields. New Graph producers already supply this object.
    """
    if not isinstance(value, str):
        return portable_tree(project, value)
    import ast

    def refuse() -> NoReturn:
        raise Held(
            cause_named(
                "migration.search",
                "unknown legacy Search representation; preserve history for review",
                owner="work.attempts",
                stage="migration",
            )
        )

    try:
        tree = ast.parse(value, mode="eval").body
    except SyntaxError:
        refuse()
        return None
    fields = {"quote_roots", "include_roots", "system_roots", "forced", "macros", "recipe"}
    if not isinstance(tree, ast.Call) or not isinstance(tree.func, ast.Name) or tree.func.id != "Search" or tree.args:
        refuse()
    if {arg.arg for arg in tree.keywords} != fields or len(tree.keywords) != len(fields):
        refuse()
    result: dict[str, Any] = {}
    for arg in tree.keywords:
        name, node = arg.arg, arg.value
        assert name is not None
        if name == "recipe":
            if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
                refuse()
            result[name] = node.value
            continue
        if not isinstance(node, ast.Tuple):
            refuse()
        values = []
        for item in node.elts:
            if name in {"quote_roots", "include_roots", "system_roots"}:
                if (
                    not isinstance(item, ast.Call)
                    or not isinstance(item.func, ast.Name)
                    or item.func.id != "PosixPath"
                    or item.keywords
                    or len(item.args) != 1
                ):
                    refuse()
                item = item.args[0]
                if not isinstance(item, ast.Constant) or not isinstance(item.value, str):
                    refuse()
                path = Path(item.value)
                if not path.is_absolute():
                    refuse()
                values.append(
                    project.id + ":" + path.relative_to(project.root).as_posix()
                    if path.is_relative_to(project.root)
                    else "external:" + path.as_posix().lstrip("/")
                )
            else:
                if not isinstance(item, ast.Constant) or not isinstance(item.value, str):
                    refuse()
                values.append(item.value)
        result[name] = values
    return result


def canonical_dependency_document(project: Project, value: Mapping[str, Any]) -> dict[str, Any]:
    raw_search = value["values"].get("search")
    search = canonical_search(project, raw_search) if raw_search is not None else None
    roots = []
    if isinstance(raw_search, str):
        import ast

        expression = ast.parse(raw_search, mode="eval").body
        assert isinstance(expression, ast.Call)
        for group in ("include_roots", "quote_roots", "system_roots"):
            node = next(arg.value for arg in expression.keywords if arg.arg == group)
            assert isinstance(node, ast.Tuple)
            for item in node.elts:
                assert isinstance(item, ast.Call) and isinstance(item.args[0], ast.Constant)
                assert isinstance(item.args[0].value, str)
                path = Path(item.args[0].value)
                if path not in roots:
                    roots.append(path)
    renamed = {}
    files = []
    for pin in value["files"]:
        row = portable_tree(project, pin)
        for label in ("path", "link_target"):
            logical = pin.get(label)
            if logical is None or logical["root"] != "external":
                continue
            physical = Path("/").joinpath(*logical["parts"])
            matched = next(((i, root) for i, root in enumerate(roots) if physical.is_relative_to(root)), None)
            if matched is None:
                raise Held(
                    cause_named(
                        "migration.scope",
                        "external legacy input has no evidenced ordered search root",
                        owner="work.attempts",
                        stage="migration",
                    )
                )
            i, root = matched
            replacement: dict[str, Any] = {"root": "include" + str(i), "parts": list(physical.relative_to(root).parts)}
            row[label] = replacement
            renamed[logical["root"] + ":" + "/".join(logical["parts"])] = (
                replacement["root"] + ":" + "/".join(replacement["parts"])
            )
        files.append(row)
    values = portable_tree(project, value["values"])
    if search is not None:
        # Project roots keep the project namespace; external roots use ordered include aliases.
        for field in ("include_roots", "quote_roots", "system_roots"):
            search[field] = [
                "include" + str(roots.index(Path("/") / name.removeprefix("external:"))) + ":"
                if name.startswith("external:")
                else name
                for name in search[field]
            ]
        values["search"] = search
    return {
        "files": files,
        "values": values,
        "recipes": portable_tree(project, value["recipes"]),
        "_watch_remap": renamed,
    }


def portable_tree(project: Project, value: Any) -> Any:
    from unbake.config import relative_text

    if isinstance(value, Mapping):
        if set(value) == {"files", "values", "recipes"}:
            result = canonical_dependency_document(project, value)
            result.pop("_watch_remap")
            return result
        result = {str(k): portable_tree(project, v) for k, v in value.items()}
        if "dependency_set" in value and "retry" in value:
            canonical = canonical_dependency_document(project, value["dependency_set"])
            remap = canonical.pop("_watch_remap")
            result["dependency_set"] = canonical
            result["retry"]["watch"] = [remap.get(watch, watch) for watch in value["retry"]["watch"]]
        return result
    if isinstance(value, (list, tuple)):
        return [portable_tree(project, v) for v in value]
    if isinstance(value, Path):
        value = str(value)
    if isinstance(value, str):
        value = value.replace(str(project.root) + "/", "project:")
        value = re.sub(r"(?<=-I)/[^\s\"']+|(?<=-L)/[^\s\"']+", lambda m: relative_text(project.root, m[0]), value)
        return relative_text(project.root, value)
    return value


def portable_value(project: Project, value: Any) -> Any:
    from unbake.config import relative_text

    if isinstance(value, Mapping):
        if set(value) == {"schema", "cause", "chain"} and value["schema"] == 2:
            return portable_fault(project, Fault.read(value)).document()
        return {k: portable_value(project, v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [portable_value(project, v) for v in value]
    if isinstance(value, str):
        return relative_text(project.root, value)
    return value
