"""Read-only cwd discovery and one terminal receipt on both output streams."""

import io
import os
import shlex
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from tests.cli.support import MainCase
from unbake.cli import common
from unbake.cli.main import main
from unbake.project import config, init
from unbake.project.config import Held


class GuidanceTests(MainCase):
    def test_discovery_uses_nearest_config_in_nested_directories(self) -> None:
        shell = self.directory / "cake"
        init.run(shell)
        nested = shell / "sub/dir"
        nested.mkdir(parents=True)
        with patch.object(Path, "cwd", return_value=nested):
            self.assertEqual(config.discover(), shell)
            code, out, error = self.run_main(["setup"], load_project=False)
        self.assertEqual(code, 1)
        self.assertIn("setup.roms", out)
        self.assertEqual(error, "")
        self.assertEqual(config.discover(nested), shell)
        with self.assertRaisesRegex(Held, "project.root"):
            config.discover(self.directory)

    def test_next_is_read_only_and_command_quotes_an_external_project(self) -> None:
        shell = self.directory / "path with spaces"
        init.run(shell)
        before = {path.relative_to(shell): path.read_bytes() for path in shell.rglob("*") if path.is_file()}
        code, out, error = self.run_main(["--project", str(shell), "next"], load_project=False)
        self.assertEqual(code, 0)
        self.assertEqual(error, "")
        action = out.split("Then run ", 1)[1].removesuffix(".\n")
        self.assertEqual(shlex.split(action), ["unbake", "--project", str(shell), "setup"])
        self.assertEqual(
            before, {path.relative_to(shell): path.read_bytes() for path in shell.rglob("*") if path.is_file()}
        )

    def test_json_failures_leave_stdout_empty_and_guidance_on_stderr(self) -> None:
        code, out, error = self.run_main(
            ["--project", str(self.directory / "missing"), "rodata", "owners", "--version", "us"], load_project=False
        )
        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertIn("HELD(config)", error)
        self.assertTrue(error.splitlines()[-1].startswith("Next: "))

    def test_json_help_and_parse_errors_also_have_one_receipt(self) -> None:
        for argv in (["rodata", "owners", "--help"], ["rodata", "owners", "--bogus"]):
            out, error = io.StringIO(), io.StringIO()
            with redirect_stdout(out), redirect_stderr(error):
                try:
                    status = main(argv)
                except SystemExit as exit_status:
                    status = exit_status.code
            self.assertEqual(status, 0 if "--help" in argv else 1)
            self.assertNotIn("Next:", out.getvalue())
            self.assertEqual(sum(line.startswith("Next: ") for line in error.getvalue().splitlines()), 1)

    def test_interrupt_has_status_130_and_missing_input_overrides_suggestion(self) -> None:
        def interrupted(*args: object) -> None:
            common.suggest("unbake next")
            raise KeyboardInterrupt

        with patch("unbake.cli.setup.run", side_effect=interrupted):
            code, out, error = self.run_main(self.args("setup"))
        self.assertEqual(code, 130)
        self.assertEqual(error, "")
        self.assertIn("Next: Supply interrupted operation", out)

    def test_explicit_policy_override_is_named_in_retry(self) -> None:
        policy = self.directory / "policy with spaces.toml"
        with patch.dict(os.environ), patch("unbake.cli.setup.run", side_effect=Held("config", "policy.splat: missing")):
            code, out, error = self.run_main(["--project", str(self.root), "--policy", str(policy), "setup"])
        self.assertEqual(code, 1)
        self.assertEqual(error, "")
        action = out.split("Then run ", 1)[1].removesuffix(".\n")
        self.assertEqual(shlex.split(action), ["unbake", "--project", str(self.root), "--policy", str(policy), "setup"])
