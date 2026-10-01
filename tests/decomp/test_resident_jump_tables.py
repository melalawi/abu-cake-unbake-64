"""Draft and trial share resident table mapping and pointer bias handling."""

import io
import shutil
import struct
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import patch

from tests.decomp.test_trial import assemble, fixture
from unbake.decomp import trial
from unbake.decomp.draft_input import jump_tables
from unbake.project import build, makefile
from unbake.project.config import Policy, Project


def resident(project: Project, bias: int) -> SimpleNamespace:
    version = project.version("us")
    version.split.write_text(
        version.split.read_text().replace("  - [0x80]", "      - [0x100, bin, resident]\n  - [0x10C]")
    )
    image = version.baserom.read_bytes()
    version.baserom.write_bytes(
        image + bytes(0x100 - len(image)) + struct.pack(">III", 0x80001018 - bias, 0x80001018 - bias, 0)
    )
    return SimpleNamespace(
        resident_mappings={"us": [dict(address=0x80003000, start=0x100, end=0x10C, table_entry_bias=bias)]}
    )


class ResidentJumpTableTests(unittest.TestCase):
    def test_trial_scores_resident_bin_table(self) -> None:
        target = [0x00051080, 0x3C018000, 0x00220821, 0x8C223000, 0x00400008, 0, 0x24020001, 0x03E00008, 0, 0]
        for bias in (0, 0x80000000):
            with self.subTest(bias=bias), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                project, policy, source = fixture(root, words=target)
                recipe = resident(project, bias)

                def compiler(project: object, policy: object, source: Path, version: str, out: Path) -> Path:
                    body = (
                        ".set noreorder\n.text\n.globl alpha\n.type alpha, @function\nalpha:\n"
                        "sll $v0,$a1,2\nlui $at,%hi(pool)\naddu $at,$at,$v0\nlw $v0,%lo(pool)($at)\n"
                        "jr $v0\nnop\ncase:\naddiu $v0,$zero,1\njr $ra\nnop\nnop\n"
                        ".size alpha,.-alpha\n.section .rodata\npool:\n.word case,case\n"
                    )
                    shutil.copyfile(assemble(out.parent, "compiled", body), out)
                    return out

                with (
                    patch.object(makefile, "recipe", return_value=recipe),
                    patch.object(build, "compile_object", side_effect=compiler),
                    redirect_stdout(io.StringIO()) as output,
                ):
                    result = trial.try_draft(project, cast(Policy, policy), source, root / "scratch")
                self.assertTrue(result.identical_everywhere, output.getvalue())
                self.assertIn("ROM offset 0x100; size 0x8; resident bin resident", output.getvalue())

    def test_draft_reads_biased_resident_entries_and_stops_at_mapping_end(self) -> None:
        for bias in (0, 0x80000000):
            with self.subTest(bias=bias), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                project, _, _ = fixture(root, words=[0] * 10)
                recipe = resident(project, bias)
                assembly = "glabel alpha\nlui $at, %hi(jtbl_80003000)\n/* 000058 80001018 00000000 */ nop\n"
                with patch.object(makefile, "recipe", return_value=recipe):
                    output = jump_tables(project, "us", "alpha", assembly)
                self.assertEqual(output.count(".word .L80001018"), 2)
                self.assertEqual(output.count(".L80001018:"), 1)
                # A table can end exactly at the resident mapping boundary.
                recipe.resident_mappings["us"][0]["end"] = 0x108
                with patch.object(makefile, "recipe", return_value=recipe):
                    self.assertEqual(jump_tables(project, "us", "alpha", assembly), output)
