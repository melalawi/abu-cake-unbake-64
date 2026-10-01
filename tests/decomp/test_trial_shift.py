"""Shifted relocation scoring through real object inspection and linking."""

import shutil
import struct
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import patch

from tests.decomp.test_trial import assemble, fixture
from unbake.decomp import trial
from unbake.decomp.needs import SymbolNeed
from unbake.project import build
from unbake.project.config import Policy, Project


class ShiftedTrialTests(unittest.TestCase):
    def test_callees_use_known_addresses_without_aligned_target_calls(self) -> None:
        cases = (
            ("matching", "", "beta", 4, 0),
            ("unaligned", "addiu $v1, $zero, 7\n", "beta", 4, 0),
            ("past_target", "addiu $v1, $zero, 7\n" * 5, "beta", 4, 0),
            ("different_callee", "", "gamma", 3, 1),
        )
        for name, prefix, callee, identical, relocations in cases:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temporary:
                directory = Path(temporary)
                jump = 0x0C000000 | (0x80001010 >> 2 & 0x03FFFFFF)
                project, policy, source = fixture(directory, [jump, 0, 0x03E00008, 0], ("us", "eu-x"))
                # The second VERSION resolves beta by correspondence rather than
                # a symbol or a row bearing its shared name.
                version = project.version("eu-x")
                version.symbols.write_text(
                    "\n".join(line for line in version.symbols.read_text().splitlines() if not line.startswith("beta"))
                    + "\n"
                )
                version.split.write_text(version.split.read_text().replace("asm, beta", "asm, other"))
                data = bytearray(version.baserom.read_bytes())
                struct.pack_into(">I", data, 0x40, 0x0C000000 | (0x80202010 >> 2 & 0x03FFFFFF))
                version.baserom.write_bytes(data)

                def compiler(
                    project: Project,
                    policy: SimpleNamespace,
                    source: Path,
                    version: str,
                    out: Path,
                    prefix: str = prefix,
                    callee: str = callee,
                ) -> Path:
                    obj = assemble(
                        out.parent,
                        "compiled",
                        ".set noreorder\n.text\n.globl alpha\n.type alpha, @function\nalpha:\n"
                        + prefix
                        + f"jal {callee}\nnop\njr $ra\nnop\n.size alpha, .-alpha\n",
                    )
                    shutil.copyfile(obj, out)
                    return out

                with patch.object(build, "compile_object", side_effect=compiler):
                    result = trial.try_draft(project, cast(Policy, policy), source, directory / "scratch")
                self.assertEqual(set(result.compares), {"us", "eu-x"})
                for version_name, comparison in result.compares.items():
                    self.assertEqual(comparison.identical, identical)
                    self.assertEqual(comparison.typed["relocation"], relocations)
                    self.assertEqual(comparison.typed["inserted"], prefix.count("\n"))
                    base = 0x80001010 if version_name == "us" else 0x80202010
                    address = base + (12 if callee == "gamma" else 0)
                    self.assertTrue(
                        any(
                            isinstance(need, SymbolNeed)
                            and need.version == version_name
                            and need.name == callee
                            and need.address == address
                            for need in result.needs
                        )
                    )
                self.assertEqual(result.identical_everywhere, name == "matching")

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
