"""Public shell onboarding and retired command acceptance."""

import importlib.util
import os
import re
import subprocess
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path

from tests.process_fakes import cli_process


class CutoverSurfaceTests(unittest.TestCase):
    def setUp(self) -> None:
        from unittest.mock import patch

        from tests.process_fakes import boundary, git_init
        from unbake.project import hygiene, init

        for mock in (
            boundary(init, git_init),
            boundary(hygiene, lambda command, **kwargs: subprocess.CompletedProcess(command, 0, b"", b"")),
        ):
            mock.start()
            self.addCleanup(mock.stop)
        self.addCleanup(patch.stopall)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.source = Path(__file__).resolve().parents[2] / "src"
        self.env = dict(
            os.environ,
            PYTHONPATH=str(self.source),
            PYTHONNOUSERSITE="1",
            UNBAKE_POLICY=str(self.root / "missing-policy.toml"),
        )

    def command(self, *arguments: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
        result = cli_process(
            [sys.executable, "-m", "unbake", *arguments],
            cwd=cwd or self.root,
            env=self.env,
            input="",
            capture_output=True,
            text=True,
            check=False,
            timeout=15,
        )
        output = result.stdout + result.stderr
        self.assertNotIn("Traceback", output)
        self.assertEqual(len(re.findall(r"(?m)^Next: .+$", output)), 1, output)
        stream = result.stderr if "Next:" in result.stderr else result.stdout
        self.assertTrue(stream.rstrip().splitlines()[-1].startswith("Next: "), output)
        return result

    def test_init_generates_staged_onboarding_without_policy_or_commit(self) -> None:
        project = self.root / "A name with spaces"
        result = self.command("init", str(project))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse((self.root / "missing-policy.toml").exists())
        self.assertFalse((project / "build").exists())
        with (project / "config.toml").open("rb") as source:
            config = tomllib.load(source)
        self.assertEqual(config["project"]["state"], "awaiting-roms")
        self.assertNotIn("compilers", config)
        self.assertNotIn("units", config)
        expected = (self.source / "unbake/project_tools/CONTRIBUTING.pending.md").read_bytes()
        self.assertEqual((project / "CONTRIBUTING.md").read_bytes(), expected)
        self.assertNotIn("toolkit", expected.decode().lower())
        for heading in ("Install", "New project", "Next command"):
            self.assertIn("## " + heading, expected.decode())
        self.assertFalse((project / ".git/HEAD").exists())
        self.assertIn("/roms/", (project / ".gitignore").read_text())
        result = self.command("setup", cwd=project)
        self.assertEqual(result.returncode, 1)
        self.assertIn("setup.roms:", result.stdout)

    def test_retired_spellings_are_parse_refusals(self) -> None:
        for operands in (
            ("init", "new", "--rom", "absent.z64"),
            ("init", "new", "--rompath", "absent"),
            ("init", "new", "--split", "files"),
            ("setup", "--new", "absent.z64"),
            ("rodata", "migrate", "alpha"),
            ("decomp", "draft", "alpha"),
            ("decomp", "try", "alpha.c"),
            ("match", "submit", "alpha.c"),
            ("match", "run"),
        ):
            with self.subTest(operands=operands):
                result = self.command(*operands)
                self.assertEqual(result.returncode, 1)
                self.assertRegex(result.stdout + result.stderr, "invalid choice|unrecognized arguments")
        self.assertFalse((self.root / "new").exists())

    def test_converter_modules_and_alternate_wrappers_are_absent(self) -> None:
        for module in (
            "unbake.layout.rodata_migrate",
            "unbake.layout.rodata_bulk",
            "unbake.layout.rodata_switch",
            "unbake.decomp.ledger",
            "unbake.match.free",
            "unbake.match.queue",
            "unbake.match.proof",
            "unbake.project.compiler_ties",
        ):
            with self.subTest(module=module):
                self.assertIsNone(importlib.util.find_spec(module))
        from unbake.cli import rodata
        from unbake.decomp import declarations, similar

        for module in (rodata, declarations, similar):
            with self.subTest(module=module.__name__):
                self.assertFalse(hasattr(module, "main"))
