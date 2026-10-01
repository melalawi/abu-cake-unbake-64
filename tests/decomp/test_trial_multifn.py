"""Named trial bodies retain their private compiled helpers."""

import io
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import patch

from tests.decomp import test_trial as support
from unbake.decomp import trial
from unbake.project import build
from unbake.project.config import Policy, Project


class PrivateHelperTests(unittest.TestCase):
    def test_private_helpers_before_after_and_in_separate_sections(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            project, policy, source = support.fixture(directory, versions=("us", "eu"))
            words = [0x24020001, 0x03E00008, 0]
            for position in ("before", "after", "section"):
                with self.subTest(position=position):
                    helper = support.assembly("helper", [0x24020007, 0x03E00008, 0]).replace(
                        ".globl helper", ".local helper"
                    )
                    if position == "section":
                        helper = helper.replace(".text", '.section .text.helper,"ax",@progbits')
                    body = support.assembly("alpha", words)
                    program = helper + body if position == "before" else body + helper

                    def compiler(
                        project: Project,
                        policy: SimpleNamespace,
                        source: Path,
                        version: str,
                        out: Path,
                        program: str = program,
                    ) -> Path:
                        built = support.assemble(out.parent, "compiled", program)
                        shutil.copyfile(built, out)
                        return out

                    with patch.object(build, "compile_object", side_effect=compiler), redirect_stdout(io.StringIO()):
                        result = trial.try_draft(project, cast(Policy, policy), source, directory / "scratch")
                    self.assertTrue(result.identical_everywhere)
                    self.assertEqual(set(result.compares), {"us", "eu"})
                    self.assertEqual(result.compares["us"].identical, 3)
