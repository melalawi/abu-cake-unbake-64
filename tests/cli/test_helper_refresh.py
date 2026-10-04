"""Incremental helper setup restores recipes without invoking setup proof."""

import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from unbake.cli import setup as cli
from unbake import config
from unbake.project import setup
from unbake.config import Held


class HelperRefreshTests(unittest.TestCase):
    def args(self, **changes):
        defaults = dict(
            refresh_helpers=True,
            compilers=False,
            repropose_compilers=False,
            replan_symbols=False,
            compiler=[],
            confirm=None,
            name=None,
            title=None,
            names_from=None,
            version_name=[],
            version_order=None,
            supply=None,
        )
        return SimpleNamespace(**(defaults | changes))

    def test_ready_refresh_uses_only_saved_recipe_and_no_proof_policy(self):
        project = SimpleNamespace(state="ready", root=Path("project"))
        with (
            patch.object(config, "load", return_value=project),
            patch.object(config, "load_policy", side_effect=AssertionError("full setup policy")),
            patch("unbake.layout.map.ensure"),
            patch.object(setup, "refresh_helpers") as refresh,
            patch.object(setup, "refresh", side_effect=AssertionError("full setup proof")),
            patch.object(cli, "receipt", return_value=False),
            patch.object(cli, "suggest") as suggest,
        ):
            self.assertFalse(cli.run(self.args(), project))
        refresh.assert_called_once_with(project)
        suggest.assert_called_once_with(cli.command(project.root, "next"))

    def test_refresh_rejects_unready_or_conflicting_setup_options(self):
        cases = [
            ("awaiting-roms", {}),
            ("ready", {"repropose_compilers": True}),
            ("ready", {"name": "other"}),
            ("ready", {"supply": Path("supply")}),
            ("ready", {"confirm": "pin"}),
        ]
        for state, changes in cases:
            with self.subTest(state=state, changes=changes), patch.object(setup, "refresh_helpers") as refresh:
                with self.assertRaises(Held):
                    cli.run(self.args(**changes), SimpleNamespace(state=state))
                refresh.assert_not_called()
