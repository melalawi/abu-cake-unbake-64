"""Dependency-bound refusals survive commands and only a watched transition permits work."""

from dataclasses import replace
from unittest.mock import patch

from tests.project_fixture import ProjectCase
from unbake import steps
from unbake.config import Held
from unbake.inputs import DependencySet
from unbake.process import Action, Fault, RetryRule, named
from unbake.work.attempts import Ledger, Operation, Outcome, RetryScope, command_ledger


class RetryTests(ProjectCase):
    def dependencies(self, cap=512_000_000, parser="a" * 64, editor="vim"):
        return DependencySet(
            (), {"memory_worker_bytes": cap, "closure_key": "b" * 64, "editor": editor}, {"parser": parser}
        )

    def hold(self, dependencies):
        with command_ledger(self.project) as history:
            operation = Operation.make(self.project, "draft", "beta", {}, dependencies)
            cause = named(
                "worker.memory",
                "result serialization exceeded worker reservation",
                owner="pool",
                stage="worker-result-serialization",
                subject="beta",
                dependencies=dependencies,
                retry=RetryRule("dependencies", ("value:memory_worker_bytes", "value:closure_key", "recipe:parser")),
                action=Action("command", ("draft", "beta")),
                evidence={"configured_cap_bytes": 512_000_000, "observed_bytes": None},
            )
            history.record(operation, Outcome(operation.id, "blocked", {}, Fault(cause), {"worker.calls": 1}))
            return operation

    def test_reopen_unrelated_host_change_is_zero_work_no_duplicate_event(self):
        initial = self.dependencies()
        self.hold(initial)
        target = self.project.root / "attempts.jsonl"
        before = target.read_bytes()
        history = Ledger(self.project)
        operation = Operation.make(self.project, "draft", "beta", {}, self.dependencies(editor="nano"))
        decision = history.retry(operation, operation.dependencies)
        self.assertEqual((decision.allowed, decision.changed, decision.reused), (False, (), True))
        calls = 0
        with self.assertRaises(Held) as caught, RetryScope(self.project, "draft", "beta", {}, operation.dependencies):
            calls += 1
        self.assertEqual(calls, 0)
        self.assertEqual(caught.exception.data["work"], {"retries": 0, "native_calls": 0, "worker_calls": 0})
        self.assertEqual(target.read_bytes(), before)

    def test_cap_parser_and_physical_input_changes_each_unlock_once(self):
        self.hold(self.dependencies())
        for expected, dependencies in (
            ("value:memory_worker_bytes", self.dependencies(cap=600_000_000)),
            ("recipe:parser", self.dependencies(parser="c" * 64)),
            (
                "value:closure_key",
                replace(self.dependencies(), values={**self.dependencies().values, "closure_key": "c" * 64}),
            ),
        ):
            operation = Operation.make(self.project, "draft", "beta", {}, dependencies)
            decision = Ledger(self.project).retry(operation, dependencies)
            self.assertEqual((decision.allowed, decision.changed), (True, (expected,)))

    def test_unknown_dependencies_are_never_a_permanent_negative_cache(self):
        deps = DependencySet((), {"dependencies_unknown": True}, {})
        self.hold(deps)
        op = Operation.make(self.project, "draft", "beta", {}, deps)
        self.assertTrue(Ledger(self.project).retry(op, deps).allowed)

    def test_worker_generation_cannot_clear_a_known_memory_cap(self):
        deps = self.dependencies()
        self.hold(deps)
        changed = replace(deps, values={**deps.values, "worker_generation": 42})
        op = Operation.make(self.project, "draft", "beta", {}, changed)
        self.assertFalse(Ledger(self.project).retry(op, changed).allowed)

    def test_vanished_file_is_not_replayed_without_a_pin_transition(self):
        calls = []

        def read():
            calls.append(1)
            raise FileNotFoundError(2, "disappeared", str(self.project.src / "beta.c"))

        with self.assertRaises(FileNotFoundError):
            steps._reading_current(self.project, read)
        self.assertEqual(calls, [1])

    def test_board_redraft_flag_cannot_erase_an_unchanged_prerequisite(self):
        deps = self.dependencies()
        self.hold(deps)
        op = Operation.make(self.project, "draft", "beta", {"replace": True}, deps)
        self.assertFalse(Ledger(self.project).retry(op, deps).allowed)

    def test_worker_produces_typed_terminal_but_only_coordinator_writes_history(self):
        from unbake.cycle import engine

        operation = Operation.make(self.project, "compare", "beta", {}, self.dependencies())

        def worker(arguments):
            self.assertFalse((self.project.root / "attempts.jsonl").exists())
            return {"ok": True, "seconds": 0.0, "source_scans": 1}

        outcome = engine.execute_task((operation, worker, ()))
        self.assertEqual((outcome.operation_id, outcome.state, outcome.value["source_scans"]), (operation.id, "ok", 1))
        self.assertFalse((self.project.root / "attempts.jsonl").exists())
        from concurrent.futures import Future

        result = Future()
        result.set_result(outcome)
        self.assertEqual(engine._result(result, operation, self.project)["source_scans"], 1)
        self.assertEqual(len(Ledger(self.project).batch(operation.id)), 1)

    def test_public_next_blocked_reads_causes_without_ranking_or_native_work(self):
        import argparse
        import io

        from unbake.cli import next as next_verb
        from unbake.cli.args import Context

        self.hold(self.dependencies())
        context = Context(
            "next",
            argparse.Namespace(blocked=True, undrafted=False),
            self.project.root,
            None,
            io.StringIO(),
            self.host,
            self.project,
        )
        with (
            patch("unbake.work.plan.next_action") as ranks,
            patch("unbake.steps.ensure") as solves,
            patch("subprocess.run") as native,
        ):
            result = next_verb.run(context)
        self.assertEqual((ranks.call_count, solves.call_count, native.call_count), (0, 0, 0))
        blocked = result.document()["data"]["blocked"]
        self.assertEqual(len(blocked), 1)
        self.assertEqual(
            (blocked[0]["key"], blocked[0]["retry_allowed"], blocked[0]["resource"]["configured_cap_bytes"]),
            ("worker.memory", False, 512_000_000),
        )
        self.assertIn("draft beta", blocked[0]["resume_command"])
