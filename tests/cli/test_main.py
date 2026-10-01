import builtins
import io
from contextlib import redirect_stdout
from typing import Any
from unittest.mock import patch

from tests.cli.support import MainCase
from unbake.cli import main as cli
from unbake.project import config


class MainTests(MainCase):
    def test_registration_import_failure_has_a_terminal_receipt(self) -> None:
        original = builtins.__import__

        def unavailable(name: str, *args: Any, **kwargs: Any) -> Any:
            fromlist = args[2] if len(args) > 2 else kwargs.get("fromlist", ())
            if name == "unbake.cli" and "draft" in fromlist:
                raise ImportError("dependency.pycparser: missing installed dependency")
            return original(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=unavailable):
            code, out, error = self.run_main(["--help"], load_project=False)
        self.assertEqual(code, 1)
        self.assertIn("HELD(config): dependency.pycparser", out)
        self.assertEqual(error, "")

    def test_all_phases_and_verbs_parse(self) -> None:
        cases = (
            ("setup", ["setup"]),
            ("setup", ["setup", "--names-from", "us"]),
            ("split", ["split", "cut", "alpha", "--version", "us", "--start", "0x40", "--end", "0x4c"]),
            ("split", ["split", "data-cut", "data", "--version", "us", "--start", "64", "--end", "76", "--apply"]),
            ("split", ["split", "rename", "alpha", "beta"]),
            ("split", ["split", "place", "alpha", "--version", "us", "--address", "0x80001000"]),
            ("split", ["split", "twins", "alpha", "--version", "us"]),
            ("decomp", ["decomp", "assign", "--holder", "person", "--tier", "manual", "--count", "2"]),
            ("decomp", ["decomp", "assign", "--holder", "person", "--tier", "manual", "--function", "alpha"]),
            ("decomp", ["decomp", "release", "assignment"]),
            ("draft", ["draft", "alpha"]),
            ("try", ["try", str(self.source)]),
            ("try", ["try", str(self.source), "--flags"]),
            ("submit", ["submit", str(self.source)]),
            ("next", ["next"]),
            ("map", ["map"]),
            ("solve", ["solve"]),
            ("decomp", ["decomp", "best", "alpha"]),
            ("decomp", ["decomp", "publish", "--all"]),
            ("match", ["match", "withdraw", "alpha"]),
            ("match", ["match", "status"]),
            ("report", ["report"]),
            ("check", ["check"]),
            ("decomp", ["decomp", "guide", "alpha", "--version", "us"]),
            ("decomp", ["decomp", "plan"]),
            ("decomp", ["decomp", "similar", "alpha"]),
            (
                "decomp",
                ["decomp", "search", "alpha.c", "--method", "order", "--out", "search", "--budget-seconds", "1"],
            ),
        )
        for phase, operands in cases:
            with self.subTest(operands=operands):
                parsed = cli.make_parser().parse_args(self.args(*operands))
                self.assertEqual(parsed.phase, phase)
                self.assertEqual(parsed.project, self.root)

    def test_work_selection_uses_project_facts_without_required_scratch(self) -> None:
        parsed = cli.make_parser().parse_args(self.args("draft", "alpha"))
        self.assertEqual(parsed.function, "alpha")
        parsed = cli.make_parser().parse_args(self.args("try", str(self.source)))
        self.assertEqual(parsed.source, self.source)
        self.assertFalse(parsed.flags)

    def test_retired_nested_work_routes_refuse(self) -> None:
        for operands in (
            ("decomp", "draft", "alpha"),
            ("decomp", "try", str(self.source)),
            ("match", "submit", str(self.source)),
            ("match", "run"),
        ):
            with self.subTest(operands=operands):
                code, out, error = self.run_main(self.args(*operands))
                self.assertEqual(code, 1)
                self.assertIn("HELD(", out)
                self.assertEqual(error, "")

    def test_required_cli_values_name_the_missing_argument(self) -> None:
        cases = (
            (["setup"], "config", "project.root"),
            (self.args(), "config", "phase"),
            (self.args("split"), "split", "verb"),
            (self.args("split", "cut", "alpha"), "split", "--version"),
            (self.args("split", "place", "alpha", "--version", "us"), "split", "--address"),
            (self.args("decomp", "assign", "--tier", "manual", "--count", "1"), "decomp", "--holder"),
            (self.args("decomp", "assign", "--holder", "person", "--count", "1"), "decomp", "--tier"),
            (self.args("decomp", "assign", "--holder", "person", "--tier", "manual"), "decomp", "--count"),
            (self.args("draft"), "draft", "FUNCTION"),
            (self.args("try"), "try", "FILE"),
            (self.args("decomp", "publish"), "decomp", "--all"),
            (self.args("submit"), "submit", "FILE"),
        )
        for args, phase, field in cases:
            with self.subTest(args=args):
                code, out, error = self.run_main(args)
                self.assertEqual(code, 1)
                self.assertTrue(out.endswith("\n"))
                self.assertEqual(error, "")
                self.assertTrue(out.startswith(f"HELD({phase}): "))
                self.assertIn(field, out)
                self.assertNotIn("usage:", out)

    def test_bad_cli_values_and_conflicting_assignment_selectors(self) -> None:
        for operands in (
            ["split", "cut", "alpha", "--version", "us", "--start", "bogus", "--end", "100"],
            ["decomp", "assign", "--holder", "person", "--tier", "manual", "--count", "0"],
            ["decomp", "assign", "--holder", "person", "--tier", "manual", "--count", "1", "--function", "alpha"],
            ["setup", "--unexpected"],
        ):
            with self.subTest(operands=operands):
                code, out, _error = self.run_main(self.args(*operands))
                self.assertEqual(code, 1)
                self.assertTrue(out.startswith("HELD("))

    def test_help_does_not_load_project_or_policy(self) -> None:
        with (
            patch.object(config, "load") as load,
            patch.object(config, "load_policy") as policy,
            redirect_stdout(io.StringIO()),
            self.assertRaises(SystemExit) as raised,
        ):
            cli.main(["--help"])
        self.assertEqual(raised.exception.code, 0)
        load.assert_not_called()
        policy.assert_not_called()

    def test_missing_config_formats_held_without_traceback(self) -> None:
        code, out, error = self.run_main(["--project", str(self.directory / "absent"), "setup"], load_project=False)
        self.assertEqual(code, 1)
        self.assertIn("HELD(config):", out)
        self.assertIn("config.toml", out)
        self.assertNotIn("Traceback", error)

    def test_missing_phase_module_is_held(self) -> None:
        original_import = builtins.__import__

        def import_module(
            name: Any, globals: Any = None, locals: Any = None, fromlist: Any = (), level: Any = 0
        ) -> Any:
            if name == "unbake.project" and "setup" in fromlist:
                raise ImportError("unbake.project.setup is unavailable")
            return original_import(name, globals, locals, fromlist, level)

        with patch.object(builtins, "__import__", side_effect=import_module):
            code, out, _error = self.run_main(self.args("setup"))
        self.assertEqual(code, 1)
        self.assertIn("HELD(setup):", out)
        self.assertIn("setup", out)
