"""Saved BattleTanx accesses: byte addresses, store widths and signed reads."""

import json
import os
import tempfile
import unittest
from pathlib import Path

from pycparser import c_parser  # type: ignore[import-untyped]

from tests.decomp.support import fixture
from unbake.config import Held
from unbake.decomp import measured_storage
from unbake.typemap.declarations import clean

PAYLOADS = Path(__file__).parents[1] / "fixtures" / "battletanx_measured_storage"


class MeasuredAccessViewTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(dir=os.environ["TMPDIR"])
        self.addCleanup(temporary.cleanup)
        self.project, _, _ = fixture(Path(temporary.name).resolve(), case=self)

    def test_three_saved_width_refusals_use_store_measurements(self):
        rows = json.loads((PAYLOADS / "bt_width_repro.json").read_text())
        self.assertEqual(len(rows), 3)
        for row, spelling in zip(rows, ("int", "unsigned short", "unsigned char"), strict=True):
            with self.subTest(function=row["function"]):
                result, header = measured_storage.prepare(
                    self.project, row["function"], row["minimal_draft"], "\n".join(row["assembly_accesses"])
                )
                self.assertIsNone(header)
                self.assertIn(f"*(({spelling} *)((unsigned char *)&{row['symbol']} + offset)) = 1", result)
                self.assertEqual(result.count("opaque address transport"), 1)
                self.assertNotIn("M2C_UNK", result)
                c_parser.CParser().parse(clean(result))

    def test_real_halfword_draft_retains_signed_reads_and_unsigned_transport(self):
        source = (PAYLOADS / "bt_halfword_raw.c").read_text()
        assembly = (PAYLOADS / "bt_halfword.s").read_text()
        result, header = measured_storage.prepare(self.project, "func_80107170_us", source, assembly)
        self.assertIsNone(header)
        self.assertIn("temp_a0_2 = *((short *)((unsigned char *)&D_803B824A + temp_a1))", result)
        self.assertIn("temp_v1 = *((short *)((unsigned char *)&D_803B824A + temp_a1))", result)
        self.assertIn("var_v1 = *((unsigned short *)((unsigned char *)&D_803B824C + temp_a1))", result)
        self.assertIn(
            "*((unsigned short *)((unsigned char *)&D_803B824A + (temp_a0 * 0x28))) = "
            "*((unsigned short *)((unsigned char *)&D_803B824A + temp_a1))",
            result,
        )
        self.assertNotIn("*(&D_803B", result)
        self.assertEqual(result.count("temp_a0 * 0x28"), source.count("temp_a0 * 0x28"))
        self.assertEqual(result.count("D_803B824A"), source.count("D_803B824A"))
        self.assertEqual(measured_storage.prepare(self.project, "func_80107170_us", result, assembly)[0], result)

    def test_mixed_signed_views_require_a_conversion_before_arithmetic(self):
        assembly = "lh $v0, %lo(data)($at)\nlhu $v1, %lo(data)($at)\nsh $v0, %lo(data)($at)\n"
        source = (
            "extern M2C_UNK data; void alpha(int offset) { "
            "use((s16) *(&data + offset)); use((u16) *(&data + offset)); }"
        )
        result, _ = measured_storage.prepare(self.project, "alpha", source, assembly)
        self.assertIn("(s16) *((short *)", result)
        self.assertIn("(u16) *((unsigned short *)", result)
        for use in ("use(*(&data + offset) / 2);", "*(&data + offset) /= 2;"):
            with self.subTest(use=use), self.assertRaisesRegex(Held, "measured signed view"):
                measured_storage.prepare(
                    self.project, "alpha", "extern M2C_UNK data; void alpha(int offset) { " + use + " }", assembly
                )

    def test_store_does_not_prove_a_read_and_mixed_widths_stay_held(self):
        for source, assembly, reason in (
            ("return *(&data + offset);", "sw $v0, %lo(data)($at)\n", "data load lacks a measured width"),
            ("*(&data + offset) = 1;", "lw $v0, %lo(data)($at)\n", "data store lacks a measured width"),
            (
                "*(&data + offset) = 1;",
                "sh $v0, %lo(data)($at)\nsb $v0, %lo(data)($at)\n",
                "data store lacks a measured width",
            ),
            ("*(&data + offset) += 1;", "lh $v0, %lo(data)($at)\nsw $v0, %lo(data)($at)\n", "read/modify/write"),
        ):
            with self.subTest(source=source, assembly=assembly), self.assertRaisesRegex(Held, reason):
                measured_storage.prepare(
                    self.project, "alpha", "extern M2C_UNK data; int alpha(int offset) { " + source + " }", assembly
                )

    def test_unrelated_indirect_call_does_not_change_signed_word_arithmetic(self):
        source = "extern M2C_UNK data; int alpha(int offset) { callback(); return *(&data + offset) / 2; }"
        result, _ = measured_storage.prepare(self.project, "alpha", source, "lw $v0, %lo(data)($at)\njalr $t0\n")
        self.assertIn("return *((int *)((unsigned char *)&data + offset)) / 2", result)

    def test_nested_offsets_evaluate_once_and_literals_are_untouched(self):
        source = (
            "extern M2C_UNK data; void alpha(void) { /* *(&data + ignored) */ "
            'char *s = "*(&data + ignored)"; *(&data + (next() * 3 + 1)) = 7; }'
        )
        result, _ = measured_storage.prepare(self.project, "alpha", source, "sb $v0, %lo(data)($at)\n")
        self.assertEqual(result.count("next()"), 1)
        self.assertIn("((unsigned char *)&data + (next() * 3 + 1))", result)
        self.assertIn('/* *(&data + ignored) */ char *s = "*(&data + ignored)";', result)
