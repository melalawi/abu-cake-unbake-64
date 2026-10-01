"""Shifted relocation scoring through real object inspection and linking."""

import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import patch

from tests.decomp.test_trial import assemble, fixture
from unbake.decomp import trial
from unbake.project import build
from unbake.project.config import Policy, Project


class ShiftedTrialTests(unittest.TestCase):
    def test_shifted_relocations_score_and_only_full_identity_can_publish(self) -> None:
        cases = (
            ("identical", True, "", "", 4, "inserted", 0),
            ("inserted", True, "addiu $v1, $zero, 7\n", "", 4, "inserted", 1),
            ("unknown_shifted", False, "addiu $v1, $zero, 7\n", "", 4, "inserted", 1),
            ("wrong_address", True, "addiu $v1, $zero, 7\n", "+4", 3, "relocation", 1),
        )
        for name, declared, prefix, addend, identical, kind, count in cases:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temporary:
                directory = Path(temporary)
                project, policy, source = fixture(directory, [0x3C028000, 0x24421028, 0x03E00008, 0])
                version = project.version("us")
                version.split.write_text(
                    version.split.read_text().replace("  - [0x68]", "      - [0x68, data, global_data]\n  - [0x78]")
                )
                version.baserom.write_bytes(version.baserom.read_bytes() + bytes(16))
                if declared:
                    version.symbols.write_text(version.symbols.read_text() + "data_symbol = 0x80001028;\n")

                def compiler(
                    project: Project,
                    policy: SimpleNamespace,
                    source: Path,
                    version: str,
                    out: Path,
                    prefix: str = prefix,
                    addend: str = addend,
                ) -> Path:
                    obj = assemble(
                        out.parent,
                        "compiled",
                        ".set noreorder\n.text\n.globl alpha\n.type alpha, @function\nalpha:\n"
                        + prefix
                        + f"lui $v0, %hi(data_symbol{addend})\naddiu $v0, $v0, %lo(data_symbol{addend})\n"
                        + "jr $ra\nnop\n.size alpha, .-alpha\n",
                    )
                    shutil.copyfile(obj, out)
                    return out

                with patch.object(build, "compile_object", side_effect=compiler):
                    result = trial.try_draft(project, cast(Policy, policy), source, directory / "scratch")
                comparison = result.compares["us"]
                self.assertEqual(comparison.identical, identical)
                self.assertEqual(comparison.typed[kind], count)
                self.assertEqual(result.identical_everywhere, name == "identical")
                if name != "identical":
                    self.assertIn("first divergence:", trial.render(result))
                if not declared:
                    self.assertTrue(any(getattr(need, "name", "") == "data_symbol" for need in result.needs))
