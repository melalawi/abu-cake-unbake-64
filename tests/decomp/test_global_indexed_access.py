"""Real global accesses and conservative affine width transport."""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pycparser import c_parser  # type: ignore[import-untyped]

from tests.decomp.support import fixture
from unbake.config import Held
from unbake.decomp import measured_access, measured_storage
from unbake.typemap.declarations import clean

PAYLOADS = Path(__file__).parents[1] / "fixtures" / "global_indexed_access"


class GlobalIndexedAccessTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(dir=os.environ["TMPDIR"])
        self.addCleanup(temporary.cleanup)
        self.project, _, _ = fixture(Path(temporary.name).resolve(), case=self)

    def test_real_word_index_uses_measured_views_without_a_table_contract(self):
        source = (PAYLOADS / "bt_reduced.c").read_text()
        assembly = (PAYLOADS / "bt_reduced.s").read_text()
        row = json.loads((PAYLOADS / "provenance.json").read_text())["bt"]
        before = set(self.project.include[0].rglob("*"))
        with patch.object(measured_access, "views", wraps=measured_access.views) as collect:
            result, shared = measured_storage.prepare(self.project, row["function"], source, assembly)
        collect.assert_called_once_with(assembly)
        self.assertIsNone(shared)
        self.assertEqual(set(self.project.include[0].rglob("*")), before)
        self.assertEqual(measured_access.views(assembly), {row["symbol"]: ({"int"}, {"int"})})
        self.assertEqual(len([line for line in assembly.splitlines() if "*/" in line]), row["instructions"])
        self.assertIn("temp_t6 = *((int *)((unsigned char *)&D_80145DE0 + (temp_t4 * 4)))", result)
        self.assertIn("*((int *)((unsigned char *)&D_80145DE0 + (sp28 * 4))) = arg0", result)
        self.assertIn("extern unsigned char D_80145DE0; /* opaque address transport */", result)
        self.assertNotIn("M2C_UNK", result)
        self.assertNotIn("[", result)
        self.assertNotIn("void (**)", result)
        c_parser.CParser().parse(clean(result))
        self.assertEqual(measured_storage.prepare(self.project, row["function"], result, assembly)[0], result)

    def test_real_rw_numeric_displacement_proves_only_byte_read(self):
        assembly = (PAYLOADS / "rw_reduced.s").read_text()
        source = (PAYLOADS / "rw_reduced.c").read_text()
        row = json.loads((PAYLOADS / "provenance.json").read_text())["rw"]
        self.assertEqual(len(assembly.splitlines()), row["instructions"])
        self.assertEqual(measured_access.views(assembly), {row["symbol"]: ({"unsigned char"}, set())})
        result, shared = measured_storage.prepare(self.project, row["function"], source, assembly)
        self.assertIsNone(shared)
        self.assertIn("M2C_FIELD(&D_80142208_de, u8 *, 0x1D)", result)
        self.assertNotIn("M2C_UNK", result)
        self.assertIn("extern unsigned char D_80142208_de;", result)

    def test_collect_once_for_multiple_opaque_globals(self):
        source = "extern M2C_UNK left; extern M2C_UNK right; int alpha(int i) { return *(&left + i) + *(&right + i); }"
        assembly = "lw $v0, %lo(left)($at)\nlh $v1, %lo(right)($at)\n"
        with patch.object(measured_access, "views", wraps=measured_access.views) as collect:
            result, _ = measured_storage.prepare(self.project, "alpha", source, assembly)
        collect.assert_called_once_with(assembly)
        self.assertIn("*((int *)", result)
        self.assertIn("*((short *)", result)

    def test_completed_global_survives_affine_byte_arithmetic_and_copies(self):
        for copy in ("move $t2, $t0", "addu $10, $8, $0", "or $t2, $zero, $t0"):
            for advance in ("addu $t2, $t2, $a0", "addu $t2, $a0, $t2", "subu $t2, $t2, $a0"):
                with self.subTest(copy=copy, advance=advance):
                    assembly = (
                        "lui $8, %hi(data)\naddiu $t0, $t0, %lo(data)\n"
                        + copy
                        + "\n"
                        + advance
                        + "\naddiu $t2, $t2, -7\n"
                        "lh $v0, -3($10)\nsh $v0, 0($t2)\n"
                    )
                    source = (
                        "extern M2C_UNK data; int alpha(int offset) { "
                        "*(&data + (offset - 10)) = 1; return *(&data + (offset - 10)); }"
                    )
                    result, _ = measured_storage.prepare(self.project, "alpha", source, assembly)
                    self.assertIn("*((short *)((unsigned char *)&data + (offset - 10)))", result)
                    self.assertIn("*((unsigned short *)((unsigned char *)&data + (offset - 10))) = 1", result)
        self.assertEqual(measured_access.views("la $fp, data\nlb $v0, 0($30)\n"), {"data": ({"signed char"}, set())})

    def test_ambiguous_and_clobbered_addresses_do_not_measure_numeric_loads(self):
        complete = "lui $t0, %hi(data)\naddiu $t0, $t0, %lo(data)\n"
        cases = (
            "lui $t0, %hi(data)\n",
            "lui $t0, %hi(other)\naddiu $t0, $t0, %lo(data)\n",
            complete + "lui $t1, %hi(other)\naddu $t0, $t0, $t1\n",
            complete + "addu $t0, $t0, $t0\n",
            complete + "subu $t0, $a0, $t0\n",
            complete + "sll $t0, $t0, 2\n",
            complete + "andi $t0, $t0, 255\n",
            complete + "sll $t1, $t0, 2\naddu $t0, $t0, $t1\n",
            complete + "lw $t0, 0($a0)\n",
            complete + "lw $t0, 0($t0)\n",
            complete + ".Ljoin:\n",
            complete + "bnez $a0, .Ljoin\nnop\n",
            complete + "jal callback\nnop\n",
        )
        for prefix in cases:
            with self.subTest(prefix=prefix):
                result = measured_access.views(prefix + "lhu $v0, 2($t0)\n")
                self.assertFalse(any("unsigned short" in reads for reads, _ in result.values()))
        self.assertEqual(measured_access.views(complete + "addiu $t0, $t0, %lo(data)\nlw $v0, 0($t0)\n"), {})
        self.assertEqual(measured_access.views("lui $t0, %hi(other)\nlw $v0, %lo(data)($t0)\n"), {})

    def test_delay_slot_has_local_evidence_but_following_path_does_not(self):
        assembly = "la $t0, data\nbeqz $a0, .Ljoin\nlh $v0, 0($t0)\nlbu $v1, 1($t0)\n"
        self.assertEqual(measured_access.views(assembly), {"data": ({"short"}, set())})

    def test_indexed_views_keep_signed_and_width_ambiguity_guards(self):
        prefix = "la $t0, data\nsll $t1, $a0, 2\naddu $t0, $t0, $t1\n"
        for access, assembly, reason in (
            ("return *(&data + offset);", "sw $v0, 0($t0)\n", "data load lacks a measured width"),
            ("*(&data + offset) = 1;", "lw $v0, 0($t0)\n", "data store lacks a measured width"),
            ("return *(&data + offset);", "lh $v0, 0($t0)\nlb $v1, 0($t0)\n", "measured width"),
            ("return *(&data + offset) / 2;", "lh $v0, 0($t0)\nlhu $v1, 0($t0)\n", "measured signed view"),
            ("*(&data + offset) += 1;", "lh $v0, 0($t0)\nsw $v0, 0($t0)\n", "read/modify/write"),
        ):
            with self.subTest(access=access, assembly=assembly), self.assertRaisesRegex(Held, reason):
                measured_storage.prepare(
                    self.project,
                    "alpha",
                    "extern M2C_UNK data; int alpha(int offset) { " + access + " }",
                    prefix + assembly,
                )
        source = (
            "extern M2C_UNK data; void alpha(int offset) { "
            "use((s16) *(&data + offset)); use((u16) *(&data + offset)); }"
        )
        result, _ = measured_storage.prepare(
            self.project, "alpha", source, prefix + "lh $v0, 0($t0)\nlhu $v1, 0($t0)\n"
        )
        self.assertIn("(s16) *((short *)", result)
        self.assertIn("(u16) *((unsigned short *)", result)

    def test_comments_and_symbol_offsets_do_not_invent_other_accesses(self):
        assembly = (
            "/* lw $v0, %lo(fake)($at) */\n"
            "/* address bytes */ lui $t0, %hi(data + 4)\n"
            "/* address bytes */ lw $v0, %lo(data + 4)($t0) # lh $v0, %lo(fake)($at)\n"
        )
        self.assertEqual(measured_access.views(assembly), {"data": ({"int"}, set())})
