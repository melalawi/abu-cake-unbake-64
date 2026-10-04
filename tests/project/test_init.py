"""Repository shell creation without policy, ROM, author or automatic commit."""

import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from unbake import config
from unbake.project import init
from unbake.config import Held


class InitTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()

    def test_shell_accepts_arbitrary_names_without_host_inputs(self) -> None:
        for name in ("cake-one", "A folder with spaces", "作品"):
            with self.subTest(name=name), patch.dict(os.environ, UNBAKE_POLICY=str(self.root / "absent")):
                target = self.root / name
                from tests.kit import boundary, git_init
                from unbake.project import hygiene

                with (
                    boundary(init, git_init),
                    boundary(hygiene, lambda command, **kwargs: subprocess.CompletedProcess(command, 0, b"", b"")),
                ):
                    init.run(target, layout_cap=2)
                project = config.load_pending(target)
                self.assertEqual(project.state, "awaiting-roms")
                self.assertEqual(
                    set(child.name for child in target.iterdir()),
                    {
                        ".git",
                        ".gitignore",
                        "roms",
                        "config.toml",
                        "layout.toml",
                        "README.md",
                        "CONTRIBUTING.md",
                    },
                )
                self.assertFalse((self.root / "absent").exists())
                self.assertNotIn("compiler", (target / "config.toml").read_text())
                self.assertIn("roms/", (target / ".gitignore").read_text())
                self.assertFalse((target / ".git/HEAD").exists())

    def test_nonempty_and_symlink_targets_preserve_inputs(self) -> None:
        existing = self.root / "existing"
        existing.mkdir()
        (existing / "keep").write_text("keep")
        link = self.root / "link"
        link.symlink_to(existing, target_is_directory=True)
        for target in (existing, link, link / "child"):
            with self.subTest(target=target), self.assertRaisesRegex(Held, "init.target"):
                from tests.kit import boundary, git_init
                from unbake.project import hygiene

                with (
                    boundary(init, git_init),
                    boundary(hygiene, lambda command, **kwargs: subprocess.CompletedProcess(command, 0, b"", b"")),
                ):
                    init.run(target, layout_cap=2)
        self.assertEqual((existing / "keep").read_text(), "keep")

    def test_git_failure_restores_an_existing_empty_directory(self) -> None:
        target = self.root / "empty"
        target.mkdir()
        with (
            patch("unbake.project.init.subprocess.run", return_value=subprocess.CompletedProcess([], 1, "", "failed")),
            self.assertRaisesRegex(Held, "git"),
        ):
            init.run(target, layout_cap=2)
        self.assertTrue(target.is_dir())
        self.assertEqual(list(target.iterdir()), [])
