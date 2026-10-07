"""The canonical repository check accepts only the current explicit host contract."""

import os
import shutil
import subprocess
from pathlib import Path

import toml

from tests.kit import TempCase, host_values


class CanonicalCheckContractTests(TempCase):
    def setUp(self):
        super().setUp()
        repository = Path(__file__).resolve().parents[2]
        (self.root / "ci").mkdir()
        shutil.copy2(repository / "ci/check", self.root / "ci/check")
        shutil.copy2(repository / "ci/policy", self.root / "ci/policy")
        (self.root / "src").symlink_to(repository / "src", target_is_directory=True)
        (self.root / ".venv").symlink_to(repository / ".venv", target_is_directory=True)
        (self.root / "tmp").mkdir()
        self.host = self.root / "host.toml"
        self.host.write_text(toml.dumps(host_values(self.root)))
        self.environment = dict(os.environ, TMPDIR=str(self.root / "tmp"), UNBAKE_CONFIG=str(self.host))
        for name in ("test", "lint", "hygiene"):
            path = self.root / "bin" / name
            path.write_text(f'#!/bin/sh\nprintf "{name}\\n" >> "$TMPDIR/checks"\nexit 0\n')
            path.chmod(0o755)

    def check(self, *args):
        return subprocess.run(
            [str(self.root / "ci/check"), *args],
            cwd=self.root,
            env=self.environment,
            text=True,
            capture_output=True,
            check=False,
        )

    def test_zero_arg_current_host_check_runs_all_existing_checks(self):
        result = self.check()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.root / "tmp/checks").read_text().splitlines(), ["test", "lint", "hygiene"])

    def test_failed_test_still_reports_lint_and_hygiene_and_fails_closed(self):
        path = self.root / "bin/test"
        path.write_text(path.read_text().replace("exit 0", "exit 1"))
        result = self.check()
        self.assertEqual(result.returncode, 1)
        self.assertEqual((self.root / "tmp/checks").read_text().splitlines(), ["test", "lint", "hygiene"])

    def test_retired_policy_cannot_authorize_the_check(self):
        self.environment.pop("UNBAKE_CONFIG")
        self.environment["UNBAKE_POLICY"] = str(self.host)
        result = self.check()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("UNBAKE_CONFIG", result.stderr)
        self.assertFalse((self.root / "tmp/checks").exists())

    def test_argument_or_unowned_scratch_is_refused_before_running_checks(self):
        self.assertNotEqual(self.check("src").returncode, 0)
        self.environment["TMPDIR"] = "relative"
        self.assertNotEqual(self.check().returncode, 0)
        self.assertFalse((self.root / "tmp/checks").exists())
