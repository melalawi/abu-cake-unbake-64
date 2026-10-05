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

    def test_step_key_follows_inputs_and_schema_not_tool_code(self) -> None:
        from unittest.mock import patch

        from unbake.typemap import mapping, storage

        def key(inputs: dict[str, str], schema: int) -> str:
            with patch.object(storage, "map_inputs", return_value=inputs), patch.object(mapping, "SCHEMA", schema):
                return steps.STEPS["rom-facts"].key(self.project, SimpleNamespace())

        base = key({"config.toml": "a"}, 1)
        self.assertRegex(base, re.compile(r"^[0-9a-f]{64}$"))
        for label, inputs, schema, changes in [
            ("same inputs and schema", {"config.toml": "a"}, 1, False),
            ("an input changed", {"config.toml": "b"}, 1, True),
            ("the schema was bumped", {"config.toml": "a"}, 2, True),
        ]:
            with self.subTest(label):
                self.assertEqual(key(inputs, schema) != base, changes)


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


class SettleTests(TempCase):
    """ensure passes again while a step wrote another step's input, and refuses a key that never settles."""

    def run_steps(self, writes: dict[str, str], *, churn: bool = False) -> list[list[str]]:
        from unittest.mock import patch

        project = SimpleNamespace(build=self.root / "build")
        inputs = {"a": 0, "b": 0}

        def step(name: str) -> steps.Step:
            def run(project: object, host: object) -> None:
                if name in writes:
                    inputs[writes[name]] += 1
                if churn:
                    inputs[name] += 1

            return steps.Step(name, name, lambda project, host: str(inputs[name]), run)

        table = {"a": step("a"), "b": step("b")}
        commands = []
        with patch.object(steps, "STEPS", table), patch.object(steps, "order", lambda names: ["a", "b"]):
            for _ in range(2):
                commands.append([row.step for row in steps.ensure(project, None, ["a", "b"]) if row.ran])
        return commands

    def test_a_later_step_writing_an_earlier_input_settles_in_the_same_command(self) -> None:
        for label, writes, first in [
            ("no writes", {}, ["a", "b"]),
            ("b writes a's input", {"b": "a"}, ["a", "b", "a"]),
        ]:
            with self.subTest(label):
                (self.root / "build").mkdir(exist_ok=True)
                (self.root / "build" / "steps.json").unlink(missing_ok=True)
                self.assertEqual(self.run_steps(writes), [first, []])

    def test_a_step_rewriting_its_own_input_every_run_is_refused_by_name(self) -> None:
        with self.assertRaises(Held) as raised:
            self.run_steps({}, churn=True)
        self.assertIn("steps.a: input key changes on every run", str(raised.exception))
