"""Cycle event schema: every event validates with its required fields and refuses without them."""

import copy
import unittest

from unbake.config import Held
from unbake.cycle import events

BASE = {"v": 1, "seq": 1, "t": "2026-10-04T00:00:00Z"}
REQUIRED = {
    "cycle.start": "project versions functions workers cores memory_total_bytes cache_root stop remote branch",
    "fn.queued": "function bytes versions carryover best_percent",
    "fn.draft.start": "function",
    "fn.draft.done": "function ok file seconds",
    "fn.edit": "function file sha256",
    "fn.compare.start": "function sha256",
    "fn.compare.done": "function sha256 per_version best_percent tries seconds",
    "fn.exact": "function bytes sha256",
    "fn.landed": "function bytes versions seconds retried",
    "fn.land_failed": "function versions diagnostic returned_to_worker",
    "fn.committed": "function commit message",
    "fn.pushed": "commits remote branch ok",
    "fn.held": "function key reason next",
    "fn.failed": "function key reason next",
    "worker.crash": "pid function task signal retried",
    "worker.memory": "pid function bytes cap_bytes",
    "step.run": "step trigger seconds",
    "cycle.end": "landed landed_bytes unpushed held carryovers exit next",
}


def line(event: str, fields: list[str]) -> dict:
    return {**BASE, "event": event, **{field: "x" for field in fields}}


class EventTests(unittest.TestCase):
    def test_every_designed_event_exists_and_requires_its_fields(self) -> None:
        self.assertEqual(set(events.EVENTS), set(REQUIRED))
        for event, names in REQUIRED.items():
            with self.subTest(event):
                self.assertLessEqual(set(names.split()), set(events.EVENTS[event]) | set(BASE) | {"event"})

    def test_validate_accepts_complete_lines(self) -> None:
        for event in events.EVENTS:
            with self.subTest(event):
                events.validate(line(event, list(events.EVENTS[event])))

    def test_validate_refuses_each_missing_field(self) -> None:
        for event, fields in events.EVENTS.items():
            for missing in fields:
                if missing in BASE or missing == "event":
                    continue
                with self.subTest(event=event, missing=missing):
                    value = line(event, [f for f in fields if f != missing])
                    value.pop(missing, None)
                    with self.assertRaises(Held):
                        events.validate(value)

    def test_validate_refuses_bad_envelopes(self) -> None:
        good = line("fn.draft.start", ["function"])
        cases = [
            ("no seq", {k: v for k, v in good.items() if k != "seq"}),
            ("no time", {k: v for k, v in good.items() if k != "t"}),
            ("no event", {k: v for k, v in good.items() if k != "event"}),
            ("wrong version", {**good, "v": 2}),
            ("unknown event", {**good, "event": "fn.nonsense"}),
        ]
        for label, value in cases:
            with self.subTest(label), self.assertRaises(Held):
                events.validate(copy.deepcopy(value))
