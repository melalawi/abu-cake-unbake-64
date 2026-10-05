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

        from unbake.layout import map as layout_map
        from unbake.typemap import mapping, storage

        def key(inputs: dict[str, str], schema: int, stale: tuple[str, ...] = ()) -> str:
            with (
                patch.object(storage, "map_inputs", return_value=inputs),
                patch.object(mapping, "SCHEMA", schema),
                patch.object(layout_map, "stale", return_value=stale),
            ):
                return steps.STEPS["rom-facts"].key(self.project, SimpleNamespace())

        base = key({"config.toml": "a"}, 1)
        self.assertRegex(base, re.compile(r"^[0-9a-f]{64}$"))
        for label, inputs, schema, stale, changes in [
            ("same inputs and schema", {"config.toml": "a"}, 1, (), False),
            ("an input changed", {"config.toml": "b"}, 1, (), True),
            ("the schema was bumped", {"config.toml": "a"}, 2, (), True),
            ("layout.toml names a row the split lost", {"config.toml": "a"}, 1, ("func_8020402C_de",), True),
        ]:
            with self.subTest(label):
                self.assertEqual(key(inputs, schema, stale) != base, changes)


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

        project = SimpleNamespace(build=self.root / "build", root=self.root)
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


class OutputDigestTests(TempCase):
    """A step's recorded output that goes missing or changes makes that step run again; nothing else does."""

    def test_a_missing_or_changed_output_reruns_its_step(self) -> None:
        from unittest.mock import patch

        project = SimpleNamespace(build=self.root / "build", root=self.root)
        output = self.root / "include" / "data.h"
        output.parent.mkdir()

        def write(project: object, host: object) -> None:
            output.write_text("/* generated */\n")

        table = {"a": steps.Step("a", "a", lambda project, host: "1", write, (), lambda project: [output])}

        def ensure() -> list[tuple[str, str]]:
            with patch.object(steps, "STEPS", table), patch.object(steps, "order", lambda names: ["a"]):
                return [(row.step, row.trigger) for row in steps.ensure(project, None, ["a"]) if row.ran]

        for label, change, expected in [
            ("first run", lambda: None, [("a", "a")]),
            ("untouched", lambda: None, []),
            ("deleted", output.unlink, [("a", "an output is missing or changed: include/data.h")]),
            (
                "edited",
                lambda: output.write_text("edited\n"),
                [("a", "an output is missing or changed: include/data.h")],
            ),
        ]:
            with self.subTest(label):
                change()
                self.assertEqual(ensure(), expected)
                self.assertEqual(output.read_text(), "/* generated */\n")


class DamagedOutputOrderTests(TempCase):
    def test_a_step_with_a_damaged_output_runs_before_the_steps_that_read_it(self) -> None:
        from unittest.mock import patch

        project = SimpleNamespace(build=self.root / "build", root=self.root)
        output = self.root / "data.h"
        ran: list[str] = []

        def reader(project: object, host: object) -> None:
            ran.append("reader")
            if output.read_text() != "good\n":
                raise Held("steps", "reader read a damaged header")

        def writer(project: object, host: object) -> None:
            ran.append("writer")
            output.write_text("good\n")

        table = {
            "reader": steps.Step("reader", "reader", lambda project, host: output.read_text(), reader),
            "writer": steps.Step("writer", "writer", lambda project, host: "1", writer, (), lambda project: [output]),
        }
        output.write_text("good\n")
        with patch.object(steps, "STEPS", table), patch.object(steps, "order", lambda names: ["reader", "writer"]):
            steps.ensure(project, None, ["reader", "writer"])
            output.write_text("damaged\n")
            ran.clear()
            steps.ensure(project, None, ["reader", "writer"])
        self.assertEqual(ran, ["writer"])
        self.assertEqual(output.read_text(), "good\n")


class CommandJournalTests(TempCase):
    """A failed or interrupted step runs again; every completed step stands; an unexpected error names the step."""

    def ensure(self, run: object) -> None:
        from unittest.mock import patch

        project = SimpleNamespace(build=self.root / "build", root=self.root)

        def write_layout(project: object, host: object) -> None:
            (self.root / "layout.toml").write_text("inferred\n")

        table = {
            "a": steps.Step("a", "a", lambda project, host: "1", write_layout),
            "b": steps.Step("b", "b", lambda project, host: "2", run),
        }
        with patch.object(steps, "STEPS", table), patch.object(steps, "order", lambda names: ["a", "b"]):
            steps.ensure(project, None, ["a", "b"])

    def test_a_failing_step_is_forgotten_and_completed_steps_stand(self) -> None:
        (self.root / "layout.toml").write_text("authored\n")
        project = SimpleNamespace(build=self.root / "build", root=self.root)
        steps.record(project, "b", "old")

        for label, error in (("error", FileNotFoundError), ("interrupt", KeyboardInterrupt)):
            with self.subTest(label):

                def failing(project: object, host: object, error: type[BaseException] = error) -> None:
                    (self.root / "layout.toml").write_text("half-written by b\n")
                    raise error("include/common/data.h")

                with self.assertRaises((Held, KeyboardInterrupt)) as caught:
                    self.ensure(failing)
                if label == "error":
                    self.assertIn("steps.b: FileNotFoundError", str(caught.exception))
                # a published its layout with its key; b reruns next time; nothing restores an older layout.
                self.assertEqual((steps.recorded(project, "a"), steps.recorded(project, "b")), ("1", None))
                self.assertEqual((self.root / "layout.toml").read_text(), "half-written by b\n")
                self.assertEqual(list((self.root / "build" / "steps.journal").glob("*.json")), [])

    def test_a_dead_commands_running_step_is_forgotten_by_the_next(self) -> None:
        from unittest.mock import patch

        project = SimpleNamespace(build=self.root / "build", root=self.root)
        steps.record(project, "a", "1")
        steps.record(project, "b", "2")
        dead = steps.Command(project)
        dead.path = self.root / "build" / "steps.journal" / "999999-1.json"
        dead.running("b")
        with patch.object(steps.os, "kill", side_effect=ProcessLookupError):
            for stale in steps.Command.stale(project):
                stale.rollback()
        self.assertEqual((steps.recorded(project, "a"), steps.recorded(project, "b")), ("1", None))
        self.assertFalse(dead.path.exists())


class BootstrapTests(TempCase):
    def test_listed_headers_that_do_not_exist_are_not_inputs(self) -> None:
        from unittest.mock import patch

        from unbake.layout import index

        present, absent = self.root / "a.h", self.root / "common" / "data.h"
        present.write_text("\n")
        with patch.object(index, "listed", return_value=frozenset({present, absent})):
            self.assertEqual(index.headers(SimpleNamespace()), frozenset({present}))

    def test_missing_generated_headers_regenerate_before_the_solve(self) -> None:
        from unittest.mock import call, patch

        from unbake.layout import header_step
        from unbake.typemap import solver

        for absent, runs in (([], []), (["common/data.h"], [call("p", "h")])):
            with (
                self.subTest(absent=absent),
                patch.object(header_step, "missing", return_value=absent),
                patch.object(header_step, "run") as run,
                patch.object(solver, "solve") as solve,
            ):
                steps.STEPS["types"].run("p", "h")
                self.assertEqual(run.call_args_list, runs)
                solve.assert_called_once_with("p", "h")
