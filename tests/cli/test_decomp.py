from types import SimpleNamespace
from unittest.mock import Mock, patch

from tests.cli.support import MainCase
from unbake.report import progress as report


class DecompTests(MainCase):
    def test_guide_and_planner_dispatch(self) -> None:
        from unbake.decomp import guide, plan, similar

        for verb, operands, member, expected in (
            ("guide", ["alpha", "--version", "us"], "run", (self.project, "alpha", "us")),
            ("guide", ["alpha"], "run", (self.project, "alpha", None)),
            ("plan", [], "ranked", (self.project, self.policy)),
            ("similar", ["alpha"], "retrieve", (self.project, "alpha", self.project.names_from)),
            ("similar", ["alpha", "--version", "us"], "retrieve", (self.project, "alpha", "us")),
            (
                "assign",
                ["--holder", "person", "--tier", "manual", "--count", "2"],
                "assign",
                (self.project, self.policy, "person", "manual"),
            ),
        ):
            with (
                self.subTest(verb=verb),
                patch.object(
                    guide if verb == "guide" else similar if verb == "similar" else plan,
                    member,
                    autospec=True,
                    return_value="guide text" if verb == "guide" else [],
                ) as operation,
            ):
                code, _, error = self.run_main(self.args("decomp", verb, *operands))
                self.assertEqual(code, 0, error)
                operation.assert_called_once_with(
                    *expected,
                    **({"count": 2} if verb == "assign" else {"top_k": 5, "bound": 512} if verb == "similar" else {}),
                )

    def test_search_constructs_explicit_permuter_and_refuses_missing_facts(self) -> None:
        from unbake.search import core
        from unbake.search.permute import Permuter

        target = self.directory / "target.o"
        target.write_bytes(report.target_object("alpha", bytes.fromhex("2402000103e0000800000000")))
        operands = self.args(
            "decomp",
            "search",
            str(self.source),
            "--method",
            "order,permute",
            "--out",
            str(self.scratch),
            "--budget-seconds",
            "10",
        )
        flags = ["--permute-version", "us", "--permute-target", str(target), "--permute-budget", "2"]
        with patch.object(core, "run", autospec=True) as run:
            code, _, error = self.run_main(operands + flags)
            self.assertEqual(code, 0, error)
            generators = run.call_args.args[3]
            self.assertEqual(len(generators), 2)
            self.assertIsInstance(generators[1], Permuter)
            self.assertEqual(
                (generators[1].version, generators[1].target_object, generators[1].budget_seconds), ("us", target, 2.0)
            )
        cases = [
            (flags[2:], "--permute-version"),
            (flags[:2] + flags[4:], "--permute-target"),
            (flags[:4], "--permute-budget"),
            (["--permute-version", "unknown", *flags[2:]], "unknown VERSION"),
            ([*flags[:4], "--permute-budget", "nan"], "--permute-budget"),
        ]
        for selected, name in cases:
            with self.subTest(name=name), patch.object(core, "run", autospec=True) as run:
                code, _, error = self.run_main(operands + selected)
                self.assertEqual(code, 1)
                self.assertIn(name, error)
                run.assert_not_called()

    def test_assign_and_release_use_project_ledger(self) -> None:
        ledger = SimpleNamespace(assign=Mock(return_value=[{"id": "assignment"}]), release=Mock())
        constructor = Mock(return_value=ledger)
        module = self.module("assign", Ledger=constructor)
        code, out, _error = self.run_main(
            self.args("decomp", "assign", "--holder", "person", "--tier", "manual", "--function", "alpha"),
            {"assign": module},
        )
        constructor.assert_called_once_with(self.project, self.policy)
        ledger.assign.assert_called_once_with("person", "manual", function="alpha")
        self.assertEqual(code, 0)
        self.assertIn('"id": "assignment"', out)
        code, out, _error = self.run_main(self.args("decomp", "release", "assignment"), {"assign": module})
        ledger.release.assert_called_once_with("assignment")
        self.assertEqual(code, 0)

    def test_best_and_publish_use_store(self) -> None:
        store = SimpleNamespace(best=Mock(return_value=self.source), publish_all=Mock(return_value=[self.source]))
        constructor = Mock(return_value=store)
        module = self.module("drafts", Store=constructor)
        code, out, _error = self.run_main(self.args("decomp", "best", "alpha"), {"drafts": module})
        store.best.assert_called_once_with("alpha")
        self.assertEqual(code, 0)
        self.assertIn(str(self.source), out)
        code, out, _error = self.run_main(self.args("decomp", "publish", "--all"), {"drafts": module})
        store.publish_all.assert_called_once_with()
        self.assertEqual(code, 0)

    def test_missing_best_names_function(self) -> None:
        store = SimpleNamespace(best=Mock(return_value=None))
        module = self.module("drafts", Store=Mock(return_value=store))
        code, _out, error = self.run_main(self.args("decomp", "best", "alpha"), {"drafts": module})
        self.assertEqual(code, 1)
        self.assertEqual(error, "HELD(decomp): no draft recorded for function alpha\n")

    def test_draft_preparation_holds_writer_lock_but_m2c_and_try_release_it(self) -> None:
        import fcntl
        from contextlib import nullcontext

        from unbake.decomp import m2c, trial, trial_compile
        from unbake.project.config import Held

        generation = self.root / "build/us.0"
        generation.mkdir(parents=True)
        self.project.build_link("us").symlink_to(generation.name)

        def assert_locked(*args: object, **kwargs: object) -> str:
            with (self.root / "build/.lock").open("a+b") as stream, self.assertRaises(BlockingIOError):
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            raise Held("proof", "lock held")

        def assert_unlocked(*args: object, **kwargs: object) -> str:
            with (self.root / "build/.lock").open("a+b") as stream:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            raise Held("proof", "lock free")

        for stage in ("make", "m2c", "try"):
            with self.subTest(stage=stage):
                verb = "try" if stage == "try" else "draft"
                operands = ["alpha", "--version", "us"] if verb == "draft" else [str(self.source)]
                with (
                    patch.object(trial_compile, "run_tool", side_effect=assert_locked if stage == "make" else None),
                    patch.object(trial, "try_draft", side_effect=assert_unlocked),
                    patch.object(trial, "trial_inputs", return_value=nullcontext({})),
                    patch.object(
                        m2c, "draft", side_effect=assert_unlocked if stage == "m2c" else None, return_value=self.source
                    ),
                    patch.object(trial, "store_trial"),
                ):
                    code, _, error = self.run_main(self.args("decomp", verb, *operands, "--scratch", str(self.scratch)))
                self.assertEqual(code, 1)
                self.assertIn("lock held" if stage == "make" else "lock free", error)
