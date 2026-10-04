"""Append-only assignments covering a function's cartridge variants."""

from __future__ import annotations

import fcntl
import json
import math
import os
import uuid
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import TextIO, TypedDict, cast

from unbake.decomp.drafts import TrialRecord
from unbake.project.config import Held, Policy, Project


class AssignmentRecord(TypedDict):
    id: str
    function: str
    versions: list[str]
    names: dict[str, str]
    holder: str
    tier: str
    at: str


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise Held("decomp", f"{label}: required nonempty value")
    return value


def _at(value: object, label: str) -> datetime:
    try:
        result = datetime.fromisoformat(cast(str, value))
        if result.tzinfo is None:
            raise ValueError
        return result
    except (TypeError, ValueError):
        raise Held("decomp", f"{label}: required timezone-aware timestamp") from None


class Ledger:
    def __init__(self, project: Project, policy: Policy) -> None:
        self.project = project
        self.policy = policy
        state_root = getattr(policy, "state_root", None)
        if not isinstance(state_root, Path):
            raise Held("decomp", "policy.state_root: required path")
        name = _text(getattr(project, "name", None), "project.name")
        if Path(name).name != name or name in (".", ".."):
            raise Held("decomp", "project.name: required single directory name")
        self.path = state_root / project.id / project.checkout_id / "assignments.jsonl"
        hours = getattr(self.policy, "assignment_idle_hours", None)
        if isinstance(hours, bool) or not isinstance(hours, (float, int)) or not math.isfinite(hours) or hours <= 0:
            raise Held("decomp", "policy.assignment_idle_hours: required positive hours")
        self.idle_hours = hours

    @contextmanager
    def _ledger(self, *, write: bool) -> Iterator[TextIO | None]:
        try:
            if write:
                self.path.parent.mkdir(parents=True, exist_ok=True)
            try:
                stream = self.path.open("a+" if write else "r", encoding="utf-8")
            except FileNotFoundError:
                if write:
                    raise
                yield None
                return
            with stream:
                fcntl.flock(stream, fcntl.LOCK_EX if write else fcntl.LOCK_SH)
                stream.seek(0)
                yield stream
        except OSError as exc:
            raise Held("decomp", f"{self.path}: {exc}") from exc

    def _active(self, stream: TextIO | None) -> list[AssignmentRecord]:
        if stream is None:
            return []
        assignments: dict[str, AssignmentRecord] = {}
        for index, line in enumerate(stream, 1):
            if not line.strip():
                continue
            label = f"{self.path}:{index}"
            try:
                row = json.loads(line)
            except ValueError as exc:
                raise Held("decomp", f"{label}: JSON") from exc
            if not isinstance(row, dict):
                raise Held("decomp", f"{label}: assignment record")
            assignment_id = _text(row.get("id"), f"{label}: id")
            _at(row.get("at"), f"{label}: at")
            if "event" in row:
                if row["event"] != "release":
                    raise Held("decomp", f"{label}: event {row['event']}")
                if assignment_id not in assignments:
                    raise Held("decomp", f"{label}: unknown assignment id {assignment_id}")
                del assignments[assignment_id]
                continue
            for key in ("function", "holder", "tier"):
                _text(row.get(key), f"{label}: {key}")
            versions, names = row.get("versions"), row.get("names")
            if (
                not isinstance(versions, list)
                or not versions
                or any(not isinstance(v, str) for v in versions)
                or not isinstance(names, dict)
                or set(names) != set(versions)
            ):
                raise Held("decomp", f"{label}: versions/names")
            for v in versions:
                self.project.version(v)
                _text(names[v], f"{label}: names.{v}")
            if assignment_id in assignments:
                raise Held("decomp", f"{label}: duplicate id {assignment_id}")
            assignments[assignment_id] = cast(AssignmentRecord, row)
        from unbake.decomp.drafts import Store

        store = Store(self.policy, self.project)
        history: dict[str, list[TrialRecord]] = {}
        for trial in store.history():
            history.setdefault(trial["function"], []).append(trial)
        now = datetime.now(UTC)
        active = []
        for row in assignments.values():
            last = _at(row["at"], f"{self.path}: at")
            for name in set((row["function"], *row["names"].values())):
                for trial in history.get(name, ()):
                    last = max(last, _at(trial.get("at"), f"draft {name}: at"))
            if (now - last).total_seconds() < self.idle_hours * 3600:
                active.append(row)
        return active

    def open(self) -> list[AssignmentRecord]:
        with self._ledger(write=False) as stream:
            return self._active(stream)

    @staticmethod
    def _append(stream: TextIO, records: Sequence[object]) -> None:
        stream.seek(0)
        content = stream.read()
        stream.seek(0, os.SEEK_END)
        if content and not content.endswith("\n"):
            stream.write("\n")
        for record in records:
            stream.write(json.dumps(record, sort_keys=True, allow_nan=False) + "\n")
        stream.flush()
        os.fsync(stream.fileno())

    def release(self, assignment_id: str) -> None:
        assignment_id = _text(assignment_id, "assignment_id")
        with self._ledger(write=True) as stream:
            assert stream is not None
            active = self._active(stream)
            if not any(row["id"] == assignment_id for row in active):
                raise Held("decomp", f"assignment_id {assignment_id}: no open assignment")
            self._append(stream, [{"id": assignment_id, "event": "release", "at": datetime.now(UTC).isoformat()}])

    def assign(self, holder: str, tier: str, *, function: str) -> list[AssignmentRecord]:
        holder, tier = _text(holder, "holder"), _text(tier, "tier")
        function = _text(function, "function")
        with self._ledger(write=True) as stream:
            assert stream is not None
            active = self._active(stream)
            from unbake.decomp import plan

            _, inventory, bodies = plan.inventory(self.project)
            groups = [items for items in plan.groups(inventory, bodies) if not any(item.kind == "c" for item in items)]
            occupied = {row["function"] for row in active}
            occupied.update(name for row in active for name in row["names"].values())
            available = [
                items
                for items in groups
                if not any(occupied.intersection((item.name, *item.aliases)) for item in items)
            ]
            selected = [items for items in available if any(function in (item.name, *item.aliases) for item in items)]
            if len(selected) != 1:
                raise Held("decomp", f"function {function}: not an unassigned unmatched function")
            now = datetime.now(UTC).isoformat()
            records: list[AssignmentRecord] = [
                {
                    "id": uuid.uuid4().hex,
                    "function": function,
                    "versions": [item.version for item in items],
                    "names": {item.version: item.name for item in items},
                    "holder": holder,
                    "tier": tier,
                    "at": now,
                }
                for items in selected
            ]
            self._append(stream, records)
            return records
