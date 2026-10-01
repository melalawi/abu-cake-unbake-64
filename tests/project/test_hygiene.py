"""Tracked project hygiene and generated ignore rules."""

import argparse
import io
import os
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from tests.project.makefile_fixture import fixture
from unbake.cli import check
from unbake.project import config, hygiene, setup


class HygieneTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(dir=os.environ["TMPDIR"])
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.project, _ = fixture(self.root)
        self.policy = config.load_policy()
        from unittest.mock import patch

        self.addCleanup(patch.stopall)
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)

    def track(self, name: str, content: bytes) -> Path:
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        subprocess.run(["git", "add", "-f", "--", name], cwd=self.root, check=True)
        return path

    def test_setup_renders_every_compiler_ignore_and_preserves_user_rules(self) -> None:
        (self.root / ".gitignore").write_text("custom/\n!tools/fixture/cc\n")
        setup.run(self.project, self.policy)
        content = (self.root / ".gitignore").read_text()
        self.assertTrue(content.startswith("custom/\n"))
        self.assertIn("/tools/fixture/\n", content)
        self.assertIn("/.splat/\n", content)
        self.assertEqual(hygiene.ignore_text(self.project), content)
        result = subprocess.run(
            ["git", "check-ignore", "tools/fixture/cc"], cwd=self.root, capture_output=True, check=False
        )
        self.assertEqual(result.returncode, 0)

    def test_check_refuses_tracked_symlink_by_name_even_when_target_missing(self) -> None:
        path = self.root / "broken-link"
        path.symlink_to("missing")
        subprocess.run(["git", "add", "broken-link"], cwd=self.root, check=True)
        with redirect_stdout(io.StringIO()) as output:
            self.assertTrue(check.run(argparse.Namespace(hygiene=True), self.project, self.policy))
        self.assertIn("broken-link: tracked symlink", output.getvalue())

    def test_ignore_merge_deduplicates_root_spellings_and_is_byte_idempotent(self) -> None:
        path = self.root / ".gitignore"
        path.write_text(
            "# Project rules\ncustom/\nbaserom.*\n/baserom.*\n__pycache__/\n/__pycache__/\n"
            "build/\n/build/\nasm/\n/asm/\n!custom/keep\n"
        )
        first = hygiene.ignore_text(self.project)
        self.assertTrue(
            first.startswith("# Project rules\ncustom/\nbaserom.*\n__pycache__/\nbuild/\nasm/\n!custom/keep\n")
        )
        path.write_text(first)
        self.assertEqual(hygiene.ignore_text(self.project).encode(), path.read_bytes())
        for name in (".unbake/cache/output", ".unbake/state/output", "tools/clone-policy.toml"):
            with self.subTest(name=name):
                ignored = subprocess.run(["git", "check-ignore", name], cwd=self.root, capture_output=True, check=False)
                self.assertEqual(ignored.returncode, 0)

    def test_check_refuses_compiler_file_even_if_force_added(self) -> None:
        setup.run(self.project, self.policy)
        self.track("tools/fixture/private.txt", b"compiler")
        self.assertEqual(
            hygiene.tracked_findings(self.project, self.policy),
            ["HELD(check): tools/fixture/private.txt: tracked compiler file (local-only directory)"],
        )

    def test_check_names_machine_paths_and_skips_binary_and_untracked_files(self) -> None:
        paths = ["/" + name + "/user/file" for name in ("home", "mnt", "opt")]
        paths.append(str(self.policy.splat))
        for number, machine_path in enumerate(paths):
            with self.subTest(machine_path=machine_path):
                name = f"machine-{number}.txt"
                tracked = self.track(name, ("portable\n" + machine_path + "\n").encode())
                tracked.write_text("clean working copy\n")
                self.assertIn(
                    f"HELD(check): {name}:2: absolute machine path",
                    hygiene.tracked_findings(self.project, self.policy),
                )
        self.track("binary.bin", b"\0" + paths[0].encode())
        (self.root / "local.txt").write_text(paths[0])
        findings = hygiene.tracked_findings(self.project, self.policy)
        self.assertFalse(any("binary.bin" in finding or "local.txt" in finding for finding in findings))
