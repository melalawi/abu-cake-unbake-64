"""Tracked project hygiene and generated ignore rules."""

import argparse
import io
import os
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
        self.root = Path(temporary.name).resolve()
        self.project, _ = fixture(self.root, case=self)
        self.policy = config.load_policy()
        from unittest.mock import patch

        self.addCleanup(patch.stopall)
        from tests.git_fixture import Index
        from tests.process_fakes import boundary

        self.index = Index(self.root)
        mock = boundary(hygiene, self.index.run)
        mock.start()
        self.addCleanup(mock.stop)

    def track(self, name: str, content: bytes) -> Path:
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        self.index.add(name)
        return path

    def test_setup_renders_every_compiler_ignore_and_preserves_user_rules(self) -> None:
        (self.root / ".gitignore").write_text("custom/\n!tools/fixture/cc\n")
        setup.run(self.project, self.policy)
        content = (self.root / ".gitignore").read_text()
        self.assertTrue(content.startswith("custom/\n"))
        self.assertIn("/tools/fixture/\n", content)
        self.assertIn("/.splat/\n", content)
        self.assertEqual(hygiene.ignore_text(self.project), content)

    def test_generated_compile_ignores_preserve_driver_sources(self) -> None:
        self.track("tools/compile/drivers/custom.py", b"source")
        content = hygiene.ignore_text(self.project)
        self.assertIn("/tools/compile/binaries/\n", content)
        self.assertIn("/tools/compile/drivers/*.sha256\n", content)
        self.assertNotIn("/tools/compile/drivers/\n", content)
        self.assertIn("*.lock\n", content)
        self.assertIn("*.lock\n", hygiene.base_ignore_text(self.root))
        (self.root / ".gitignore").write_text(content)
        self.assertEqual(hygiene.ignore_text(self.project), content)

    def test_check_refuses_tracked_symlink_by_name_even_when_target_missing(self) -> None:
        path = self.root / "broken-link"
        path.symlink_to("missing")
        self.index.add("broken-link")
        with redirect_stdout(io.StringIO()) as output:
            self.assertTrue(check.run(argparse.Namespace(hygiene=True), self.project, self.policy))
        self.assertIn("broken-link: tracked symlink", output.getvalue())

    def test_ignore_merge_deduplicates_root_spellings_and_is_byte_idempotent(self) -> None:
        path = self.root / ".gitignore"
        path.write_text(
            "# Project rules\ncustom/\nroms/\n/roms/\n__pycache__/\n/__pycache__/\n"
            "build/\n/build/\nasm/\n/asm/\n!custom/keep\n"
        )
        first = hygiene.ignore_text(self.project)
        self.assertTrue(first.startswith("# Project rules\ncustom/\nroms/\n__pycache__/\nbuild/\nasm/\n!custom/keep\n"))
        path.write_text(first)
        self.assertEqual(hygiene.ignore_text(self.project).encode(), path.read_bytes())

    def test_tracked_baserom_identity_remains_visible_and_ignore_is_idempotent(self) -> None:
        self.track("baserom.sha1", b"identity")
        ignore = self.root / ".gitignore"
        ignore.write_text(hygiene.ignore_text(self.project))
        self.assertIn("!baserom.sha1\n", ignore.read_text())
        self.assertEqual(hygiene.ignore_text(self.project), ignore.read_text())

    def test_tracked_version_identity_files_get_the_same_exception(self) -> None:
        self.track("versions/us/baserom.sha1", b"identity")
        ignore = self.root / ".gitignore"
        ignore.write_text(hygiene.ignore_text(self.project))
        self.assertIn("!baserom.sha1\n", ignore.read_text())
        self.assertEqual(hygiene.ignore_text(self.project), ignore.read_text())

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
