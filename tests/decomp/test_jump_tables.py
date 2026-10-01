"""Imperfect indexed jump tables remain scoreable without publication proof."""

import io
import shutil
import struct
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from typing import cast
from unittest.mock import patch

from tests.decomp.test_trial import READELF, assemble, fixture
from unbake.decomp import needs, trial, trial_compare, trial_link
from unbake.decomp.indexed import indexed_references, table_guidance
from unbake.decomp.trial_layout import FunctionSpan
from unbake.project import build
from unbake.project.config import Policy


class JumpTableTests(unittest.TestCase):
    def test_indexed_anchor_requires_scaled_index_and_live_constant(self) -> None:
        for code, expected in (
            ((0x00051080, 0x3C018007, 0x00220821, 0x8C221350), [0x80071350]),
            ((0x10400020, 0x00031080, 0x3C018007, 0x00220821, 0x8C221350), [0x80071350]),
            ((0x00051080, 0x3C018008, 0x00220821, 0x8C228000), [0x80078000]),
            ((0x00051080, 0x3C018007, 0x24010000, 0x00220821, 0x8C221350), []),
            ((0x3C018007, 0x00220821, 0x8C221350), []),
            ((0x00051080, 0x3C018007, 0x00220821, 0x8C210000, 0x8C221350), [0x80070000]),
        ):
            with self.subTest(code=code):
                refs = indexed_references(code)
                self.assertEqual([ref.address for ref in refs], expected)
                self.assertTrue(all(ref.scale == 4 for ref in refs))

    def test_imperfect_table_scores_shifted_equal_and_unequal_length_candidates(self) -> None:
        target = [0x00051080, 0x3C018000, 0x00220821, 0x8C221040, 0x00400008, 0, 0x24020001, 0x03E00008, 0, 0]
        for extra in (False, True):
            with self.subTest(extra=extra), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                project, policy, source = fixture(root, words=target)
                version = project.version("us")
                version.split.write_text(
                    version.split.read_text().replace("  - [0x80]", "      - [0x80, data, dispatch]\n  - [0x88]")
                )
                version.baserom.write_bytes(version.baserom.read_bytes() + struct.pack(">II", 0x80001018, 0x80001018))
                guidance = table_guidance(project, "us", "alpha", FunctionSpan(0x80001000, 0x40, 40, 4), target)
                self.assertIn("alpha owns jtbl_80001040", guidance)
                self.assertIn("ROM offset 0x80; size 0x8", guidance)
                scratch = root / "scratch"

                def compiler(
                    project: object, policy: object, source: Path, version: str, out: Path, *, extra: bool = extra
                ) -> Path:
                    body = (
                        ".set noreorder\n.text\n.globl alpha\n.type alpha, @function\nalpha:\n"
                        "nop\nsll $v0,$a1,2\nlui $at,%hi(pool)\naddu $at,$at,$v0\nlw $v0,%lo(pool)($at)\n"
                        "jr $v0\nnop\ncase:\naddiu $v0,$zero,2\njr $ra\nnop\n"
                        + ("nop\n" if extra else "")
                        + ".size alpha,.-alpha\n.section .rodata\npool:\n.word case,case\n"
                    )
                    shutil.copyfile(assemble(out.parent, "compiled", body), out)
                    return out

                with (
                    patch.object(build, "compile_object", side_effect=compiler),
                    redirect_stdout(io.StringIO()) as output,
                ):
                    result = trial.try_draft(project, cast(Policy, policy), source, scratch)
                self.assertFalse(result.identical_everywhere)
                self.assertGreater(result.compares["us"].identical, 0)
                self.assertEqual(result.compares["us"].typed["rodata"], 1)
                self.assertFalse(any(isinstance(need, needs.RodataNeed) for need in result.needs))
                self.assertTrue(any("publication placement proof" in line for line in result.preconditions))
                linked = next(scratch.glob("alpha.*/us/trial.elf"))
                output_elf = trial_link.inspect(linked, READELF, linked.parent)
                pool = next(section for section in output_elf.sections.values() if section.name == ".rodata")
                case = next(symbol for symbol in output_elf.symbols if symbol.name == "case")
                self.assertEqual(case.address, 0x8000101C)
                table = linked.read_bytes()[pool.offset : pool.offset + pool.size]
                self.assertEqual(trial_compare.words(table), [case.address, case.address])
                self.assertEqual(version.baserom.read_bytes()[-8:], struct.pack(">II", 0x80001018, 0x80001018))
                text = output.getvalue()
                self.assertIn("compiler-generated .rodata: private draft constants; text scoring only", text)
                self.assertNotIn("rodata .rodata+", text)
