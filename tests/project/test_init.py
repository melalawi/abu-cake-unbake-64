"""Repository shell creation without policy, ROM, author or automatic commit."""

import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from unbake.project import config, init
from unbake.project.config import Held


class InitTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def test_shell_accepts_arbitrary_names_without_host_inputs(self) -> None:
        for name in ("cake-one", "A folder with spaces", "作品"):
            with self.subTest(name=name), patch.dict(os.environ, UNBAKE_POLICY=str(self.root / "absent")):
                target = self.root / name
                init.run(target)
                project = config.load_pending(target)
                self.assertEqual(project.state, "awaiting-roms")
                self.assertEqual(
                    set(child.name for child in target.iterdir()),
                    {".git", ".gitignore", "roms", "config.toml", "README.md", "CONTRIBUTING.md"},
                )
                self.assertFalse((self.root / "absent").exists())
                self.assertNotIn("compiler", (target / "config.toml").read_text())
                result = subprocess.run(
                    ["git", "check-ignore", "roms/arbitrary.z64"], cwd=target, capture_output=True, text=True
                )
                self.assertEqual(result.returncode, 0)
                self.assertEqual(result.stdout, "roms/arbitrary.z64\n")
                result = subprocess.run(["git", "rev-parse", "--verify", "HEAD"], cwd=target, capture_output=True)
                self.assertNotEqual(result.returncode, 0)

    def test_nonempty_and_symlink_targets_preserve_inputs(self) -> None:
        existing = self.root / "existing"
        existing.mkdir()
        (existing / "keep").write_text("keep")
        link = self.root / "link"
        link.symlink_to(existing, target_is_directory=True)
        for target in (existing, link, link / "child"):
            with self.subTest(target=target), self.assertRaisesRegex(Held, "init.target"):
                init.run(target)
        self.assertEqual((existing / "keep").read_text(), "keep")

    def test_git_failure_restores_an_existing_empty_directory(self) -> None:
        target = self.root / "empty"
        target.mkdir()
        with (
            patch("unbake.project.init.subprocess.run", return_value=subprocess.CompletedProcess([], 1, "", "failed")),
            self.assertRaisesRegex(Held, "git"),
        ):
            init.run(target)
        self.assertTrue(target.is_dir())
        self.assertEqual(list(target.iterdir()), [])
