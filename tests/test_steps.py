"""Step keys recorded in build/steps.json."""

import re
from types import SimpleNamespace

from tests.kit import TempCase
from unbake import steps
from unbake.config import Held


class StepRecordTests(TempCase):
    def setUp(self) -> None:
        super().setUp()
        self.project = SimpleNamespace(build=self.root / "build")

    def test_record_roundtrip(self) -> None:
        self.assertIsNone(steps.recorded(self.project, "types"))
        steps.record(self.project, "types", "k1")
        steps.record(self.project, "headers", "k2")
        self.assertEqual(steps.recorded(self.project, "types"), "k1")
        self.assertEqual(steps.recorded(self.project, "headers"), "k2")
        steps.record(self.project, "types", "k3")
        self.assertEqual((steps.recorded(self.project, "types"), steps.recorded(self.project, "headers")), ("k3", "k2"))
        self.assertIsNone(steps.recorded(self.project, "obj"))

    def test_unreadable_record_is_refused_by_name(self) -> None:
        for label, text in [("not json", "{"), ("not an object", "[1]")]:
            with self.subTest(label):
                (self.root / "build").mkdir(exist_ok=True)
                (self.root / "build" / "steps.json").write_text(text)
                with self.assertRaises(Held) as raised:
                    steps.recorded(self.project, "types")
                self.assertEqual(raised.exception.phase, "steps")

    def test_tool_fingerprint_is_a_stable_digest(self) -> None:
        self.assertRegex(steps.tool_fingerprint(), re.compile(r"^[0-9a-f]{64}$"))
        self.assertEqual(steps.tool_fingerprint(), steps.tool_fingerprint())


class StepOrderTests(TempCase):
    def test_needed_steps_come_first_once(self) -> None:
        for names, expected in [
            (["types"], ["extract", "rom-facts", "types"]),
            (["extract", "types", "headers", "buildfiles"], ["extract", "rom-facts", "types", "headers", "buildfiles"]),
            (["buildfiles"], ["buildfiles"]),
            (["headers", "extract"], ["extract", "rom-facts", "types", "headers"]),
        ]:
            with self.subTest(names=names):
                self.assertEqual(steps.order(names), expected)

    def test_unknown_step_is_refused_by_name(self) -> None:
        with self.assertRaises(Held) as raised:
            steps.order(["map"])
        self.assertIn("steps.map", str(raised.exception))
