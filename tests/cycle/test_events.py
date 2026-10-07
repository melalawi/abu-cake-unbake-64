"""Cycle event schema: every event validates with its required fields and refuses without them."""

import io
import json
import unittest

from unbake.cycle import events

REQUIRED = {
    "cycle.start": "project versions functions workers cores memory_total_bytes cache_root stop",
    "fn.queued": "function bytes versions carryover best_percent",
    "fn.draft.start": "function",
    "fn.draft.done": "function ok file seconds",
    "fn.edit": "function file sha256",
    "fn.compare.start": "function sha256",
    "fn.compare.done": "function sha256 per_version best_percent tries seconds",
    "fn.search.start": "function method",
    "fn.search.done": "function method ok seconds",
    "fn.creative": "function best_percent methods trouble",
    "fn.exact": "function bytes sha256",
    "fn.landed": "function bytes versions seconds",
    "fn.fuzzy_landed": "function bytes versions commit best_percent",
    "fn.land_failed": "function versions diagnostic returned_to_worker",
    "fn.committed": "function commit message",
    "cycle.committed": "commit message functions",
    "fn.held": "function key reason next",
    "fn.failed": "function key reason next",
    "worker.crash": "pid function task signal retried",
    "worker.memory": "pid function bytes cap_bytes",
    "step.run": "step trigger seconds",
    "steps.held": "key reason",
    "fn.recheck": "function exact best_percent",
    "cycle.end": "landed landed_bytes held carryovers exit next",
}


def fields(event: str) -> dict:
    values = {
        "workers": 2,
        "cores": 2,
        "memory_total_bytes": 1000,
        "bytes": 12,
        "tries": 1,
        "landed_bytes": 12,
        "pid": 1,
        "signal": 9,
        "cap_bytes": 1000,
        "exit": 0,
        "versions": ["us"],
        "functions": ["alpha"],
        "methods": {"types": 50.0},
        "landed": [],
        "held": [],
        "carryovers": [],
        "ok": True,
        "carryover": False,
        "exact": True,
        "retried": False,
        "returned_to_worker": True,
        "seconds": 0.1,
        "best_percent": None,
        "per_version": {},
    }
    return {name: values.get(name, "x") for name in events.SCHEMA[event][0]}


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
            [(r["v"], r["seq"], r["event"]) for r in lines], [(2, 1, "fn.draft.start"), (2, 2, "fn.exact")]
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


class TypedDeliveryTests(unittest.TestCase):
    def test_malformed_fields_fail_before_sequence_or_output_changes(self):
        emitter = events.Emitter(io.StringIO())
        for fields_ in (
            {"function": 17, "key": [], "reason": {}, "next": 3},
            {"function": "a", "key": "x", "reason": "bad", "next": 3},
        ):
            with self.assertRaises(events.SchemaError):
                emitter.emit("fn.held", **fields_)
        with self.assertRaises(events.SchemaError):
            emitter.emit("fn.exact", function="a", bytes=True, sha256="s")
        with self.assertRaises(events.SchemaError):
            emitter.emit("step.run", step="types", trigger="changed", seconds=float("nan"))
        self.assertEqual((emitter.seq, emitter.stream.getvalue()), (0, ""))

    def test_concurrent_and_reentrant_listeners_observe_stdout_sequence(self):
        import threading

        entered, release = threading.Event(), threading.Event()
        emitter = events.Emitter(io.StringIO())
        seen = []

        def listener(record):
            if record["seq"] == 1:
                entered.set()
                self.assertTrue(release.wait(2))
            seen.append(record["seq"])
            if record["seq"] == 2:
                emitter.emit("fn.draft.start", function="reentrant")

        emitter.listeners.append(listener)
        thread = threading.Thread(target=lambda: emitter.emit("fn.draft.start", function="first"))
        thread.start()
        self.assertTrue(entered.wait(2))
        emitter.emit("fn.draft.start", function="second")
        release.set()
        thread.join(2)
        self.assertFalse(thread.is_alive())
        written = [json.loads(line)["seq"] for line in emitter.stream.getvalue().splitlines()]
        self.assertEqual((written, seen), ([1, 2, 3], [1, 2, 3]))
