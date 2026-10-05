"""recompute names one or more steps, or --all; anything else is refused by name."""

import argparse
from types import SimpleNamespace
from unittest.mock import patch

from tests.kit import TempCase
from unbake import steps
from unbake.cli import recompute
from unbake.config import Held


class RecomputeTests(TempCase):
    def run_with(self, argv: list[str]) -> tuple[str, ...]:
        parser = argparse.ArgumentParser()
        recompute.register(parser)
        args = parser.parse_args(argv)
        context = SimpleNamespace(args=args, project=lambda: "p", require_host=lambda: "h", cmd=lambda *w: "next")
        with patch.object(steps, "recompute", return_value=[]) as ran:
            recompute.run(context)
        return tuple(ran.call_args.args[2])

    def test_forms(self) -> None:
        for argv, expected in [
            (["headers"], ("headers",)),
            (["headers", "buildfiles", "headers"], ("headers", "buildfiles")),
            (["--all"], tuple(steps.NAMES)),
        ]:
            with self.subTest(argv=argv):
                self.assertEqual(self.run_with(argv), expected)

    def test_refusals(self) -> None:
        for argv, reason in [
            ([], "name one or more steps, or --all"),
            (["headers", "--all"], "name one or more steps, or --all"),
            (["headrs"], "unknown headrs"),
        ]:
            with self.subTest(argv=argv), self.assertRaisesRegex(Held, reason):
                self.run_with(argv)
