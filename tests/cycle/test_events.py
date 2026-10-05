"""Cycle event schema: every event validates with its required fields and refuses without them."""

import io
import json
import unittest

from unbake.cycle import events

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
    "types.refreshed": "steps seconds ok",
    "cycle.end": "landed landed_bytes unpushed held carryovers exit next",
}


def fields(event: str) -> dict:
    return dict.fromkeys(events.SCHEMA[event][0], "x")


class EventTests(unittest.TestCase):
    def test_every_designed_event_exists_with_exactly_its_required_fields(self) -> None:
        self.assertEqual(set(events.SCHEMA), set(REQUIRED))
        for event, names in REQUIRED.items():
            with self.subTest(event):
                self.assertEqual(events.SCHEMA[event][0], frozenset(names.split()))

    def test_validate_refuses_each_missing_field_and_unknown_names(self) -> None:
        for event, (required, _) in events.SCHEMA.items():
            events.validate(event, fields(event))
            for missing in required:
                with self.subTest(event=event, missing=missing), self.assertRaises(events.SchemaError):
                    events.validate(event, {k: v for k, v in fields(event).items() if k != missing})
            with self.subTest(event=event, extra=True), self.assertRaises(events.SchemaError):
                events.validate(event, {**fields(event), "surprise": 1})
        with self.assertRaises(events.SchemaError):
            events.validate("fn.nonsense", {})

    def test_emitter_writes_numbered_envelopes_and_refuses_before_writing(self) -> None:
        stream = io.StringIO()
        emitter = events.Emitter(stream)
        emitter.emit("fn.draft.start", function="alpha")
        emitter.emit("fn.exact", function="alpha", bytes=12, sha256="s")
        with self.assertRaises(events.SchemaError):
            emitter.emit("fn.exact", function="alpha")
        lines = [json.loads(line) for line in stream.getvalue().splitlines()]
        self.assertEqual(
            [(r["v"], r["seq"], r["event"]) for r in lines], [(1, 1, "fn.draft.start"), (1, 2, "fn.exact")]
        )
        self.assertTrue(all(r["t"] for r in lines))


class FirstDifferenceTests(unittest.TestCase):
    def test_first_reason_a_version_is_not_exact(self) -> None:
        from unbake.cycle.engine import first_difference

        for lines, expected in [
            (["VERSION us: cc1 exited 33: too few arguments"], "VERSION us: cc1 exited 33: too few arguments"),
            (
                ["us: 10/12 words", "typed: register 2", "first divergence: +0x0010 a != b"],
                "first divergence: +0x0010 a != b",
            ),
            (["us: 12/12 words", "constant: .rodata+0x4 differs"], "constant: .rodata+0x4 differs"),
            (["us: 12/12 words"], ""),
        ]:
            with self.subTest(lines=lines):
                self.assertEqual(first_difference(lines), expected)
