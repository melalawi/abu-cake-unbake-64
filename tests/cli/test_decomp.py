from types import SimpleNamespace
from unittest.mock import Mock, patch

from tests.cli.support import MainCase
from unbake.report import progress as report


class DecompTests(MainCase):
    def test_guide_and_planner_dispatch(self) -> None:
        from unbake.decomp import guide, plan

        for verb, operands, member, expected in (
            ("guide", ["alpha", "--version", "us"], "run", (self.project, "alpha", "us")),
            ("guide", ["alpha"], "run", (self.project, "alpha", None)),
            ("plan", [], "ranked", (self.project, self.policy)),
            ("similar", ["alpha"], "similar", (self.project, "alpha")),
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
                    guide if verb == "guide" else plan,
                    member,
                    autospec=True,
                    return_value="guide text" if verb == "guide" else [],
                ) as operation,
            ):
                code, _, error = self.run_main(self.args("decomp", verb, *operands))
                self.assertEqual(code, 0, error)
                operation.assert_called_once_with(*expected, **{"count": 2} if verb == "assign" else {})

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
        self.assertEqual(error, "HELD(decomp): best draft for function alpha: missing value\n")
