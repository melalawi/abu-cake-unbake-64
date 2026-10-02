from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from tests.cli.support import MainCase


class SplitTests(MainCase):
    def test_split_preview_never_applies(self) -> None:
        edits = [object()]
        cut = Mock(return_value=edits)
        apply = Mock()
        module = self.module("split", cut=cut, diff=Mock(return_value="--- before\n+++ after\n"), apply=apply)
        code, out, _error = self.run_main(
            self.args("split", "cut", "alpha", "--version", "us", "--start", "0x40", "--end", "0x4c"), {"split": module}
        )
        cut.assert_called_once_with(self.project, "us", "alpha", 64, 76)
        apply.assert_not_called()
        self.assertEqual(code, 0)
        self.assertIn("+++ after", out)
        self.assertIn("OK(split): preview 1 file edits", out)

    def test_split_apply_reports_failed_version(self) -> None:
        edits = [object()]
        result = SimpleNamespace(version="us", ok=False, sha1_line="FAILED", log=Path("build/us.log"))
        apply = Mock(return_value=[result])
        module = self.module("split", data_cut=Mock(return_value=edits), diff=Mock(return_value=""), apply=apply)
        code, out, _error = self.run_main(
            self.args("split", "data-cut", "table", "--version", "us", "--start", "64", "--end", "76", "--apply"),
            {"split": module},
        )
        apply.assert_called_once_with(self.project, self.policy, edits)
        self.assertEqual(code, 1)
        self.assertIn("HELD(split): VERSION us: FAILED log build/us.log", out)

    def test_split_rename_uses_simultaneous_planner(self) -> None:
        from unbake.layout import rename_map

        with (
            patch.object(rename_map, "plan", return_value=[]) as plan,
            patch.object(rename_map, "diff", return_value=""),
        ):
            code, _out, _error = self.run_main(self.args("split", "rename", "alpha", "beta"))
        plan.assert_called_once_with(self.project, {"alpha": "beta"})
        self.assertEqual(code, 0)

    def test_split_place_and_twins_signatures(self) -> None:
        for verb, operands, expected in (
            (
                "place",
                ["alpha", "--version", "us", "--address", "0x80001000"],
                (self.project, "us", "alpha", 2147487744),
            ),
            ("twins", ["alpha", "--version", "us"], (self.project, "us", "alpha")),
        ):
            operation = Mock(return_value=[])
            module = self.module("split", **{verb: operation}, diff=Mock(return_value=""))
            with self.subTest(verb=verb):
                code, _out, _error = self.run_main(self.args("split", verb, *operands), {"split": module})
                operation.assert_called_once_with(*expected)
                self.assertEqual(code, 0)
