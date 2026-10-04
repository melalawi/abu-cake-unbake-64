"""Measured storage views preserve byte offsets and reject unsupported inference."""

import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

from pycparser import c_parser  # type: ignore[import-untyped]

from tests.decomp.support import fixture
from unbake.decomp import checks, measured_storage
from unbake.decomp.draft_abi import declarations
from unbake.decomp.draft_asm import address_aliases
from unbake.decomp.draft_layouts import normalize
from unbake.decomp.field_access import share
from unbake.decomp.trial_compile import default_scratch, scratch_directory
from unbake.config import Held
from unbake.typemap.declarations import clean


class MeasuredStorageTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(dir=os.environ["TMPDIR"])
        self.addCleanup(temporary.cleanup)
        self.project, self.policy, _ = fixture(Path(temporary.name).resolve(), case=self)

    def test_alias_pair_retains_explicit_byte_delta_in_analysis(self):
        assembly = (
            "lui $at, %hi(left)\naddu $at, $at, $v0\nlwl $v1, %lo(left)($at)\n"
            "lui $at, %hi(alias + 1)\naddu $at, $at, $v0\nlwr $v1, %lo(alias + 1)($at)\n"
            "lw $a0, %lo(unrelated)($a1)\n"
        )
        result = address_aliases(assembly, {"left": 0x80008000, "alias": 0x80008002, "unrelated": 0x80009000})
        self.assertEqual(result.count("lui $at, %hi(left)"), 2)
        self.assertIn("lwl $v1, 0($at)", result)
        self.assertIn("lwr $v1, 3($at)", result)
        self.assertIn("lw $a0, %lo(unrelated)($a1)", result)
        self.assertEqual(address_aliases(result, {"left": 0x80008000}), result)
        self.assertEqual(address_aliases(assembly, {}), assembly)

    def test_shared_stack_preserves_overlapping_word_and_halfword_views(self):
        source = (
            "struct _m2c_stack_alpha {\n"
            "/* 0x10 */ M2C_UNK sp10;\n/* 0x10 */ char pad10[4];\n"
            "/* 0x14 */ s16 sp14;\n/* 0x16 */ s16 sp16;\n"
            "/* 0x18 */ M2C_UNK sp18;\n/* 0x18 */ char pad18[8];\n"
            "}; /* size = 0x20 */\n"
            "extern M2C_UNK data;\n"
            "void alpha(void) {\n M2C_UNK sp10;\n s16 sp14;\n s16 sp16;\n M2C_UNK sp18;\n"
            "sp10 = M2C_UNALIGNED32(*(&data + index));\n"
            "sp14 = M2C_UNALIGNED32(*(&data + index));\nsp16 = 2; use(&sp18);\n}\n"
        )
        output, shared = measured_storage.prepare(self.project, "alpha", source, "")
        self.assertIsNotNone(shared)
        text = shared.read_text()
        self.assertIn("unsigned int word_sp14", text)
        self.assertIn("unsigned char padding[20]; unsigned int word_sp14", text)
        self.assertIn("unsigned char padding[22]; s16 sp16", text)
        self.assertIn("unsigned char sp18[8]", text)
        self.assertIn("frame.storage.slot_word_sp14.word_sp14 =", output)
        self.assertIn("unbake_bytes_0[0] << 24", output)
        self.assertNotIn("M2C_UNK", output + text)
        c_parser.CParser().parse("typedef short s16; int index; void use(void *);\n" + clean(text + output))
        self.assertFalse(checks.run(output))

    def test_unknown_extent_and_direct_scalar_still_refuse(self):
        output, shared = measured_storage.prepare(
            self.project, "alpha", "extern M2C_UNK data; int alpha(void) { return data; }", ""
        )
        self.assertIsNone(shared)
        with self.assertRaisesRegex(Held, "no declared target layout"):
            normalize(output, "")
        source = (
            "struct _m2c_stack_alpha { /* 0x10 */ M2C_UNK sp10; }; /* size = 0x20 */ void alpha(void) { use(&sp10); }"
        )
        with self.assertRaisesRegex(Held, "lacks a measured extent"):
            measured_storage.prepare(self.project, "alpha", source, "")

    def test_addressed_data_load_without_instruction_width_refuses(self):
        source = "extern M2C_UNK data; int alpha(int offset) { return *(&data + offset); }"
        with self.assertRaisesRegex(Held, "data load lacks a measured width"):
            measured_storage.prepare(self.project, "alpha", source, "")

    def test_indirect_word_call_and_signed_halfword_keep_distinct_views(self):
        source = (
            "extern M2C_UNK table; extern M2C_UNK data; void alpha(int offset) { "
            "if (*(&table + offset)) *(&table + offset)(1); use(*(&data + offset)); }"
        )
        output, _ = measured_storage.prepare(
            self.project, "alpha", source, "lw $v0, %lo(table)($at)\njalr $v0\n lh $v1, %lo(data)($at)\n"
        )
        self.assertIn("void (**)()", output)
        self.assertIn("short *)(&data", output)
        c_parser.CParser().parse("void use(int);\n" + clean(output))

    def test_unaligned_field_width_refuses_instead_of_guessing_padding(self):
        with self.assertRaisesRegex(Held, "unaligned field"):
            share(self.project, "alpha", "int alpha(void *p) { return M2C_FIELD(p, int *, 1); }", "")

    def test_default_scratch_is_external_and_explicit_inside_still_refuses(self):
        policy = SimpleNamespace(state_root=self.project.root / ".unbake/state")
        directory = default_scratch(self.project, policy)
        self.assertFalse(directory.resolve().is_relative_to(self.project.root))
        self.assertEqual(default_scratch(self.project, policy), directory)
        self.assertEqual(
            default_scratch(self.project, replace(self.policy, state_root=self.project.work)),
            self.project.work / "draft-work",
        )
        with self.assertRaisesRegex(Held, "outside project.root"):
            scratch_directory(self.project, self.project.root / "work", "try")

    def test_explicit_solved_callee_prototype_precedes_unknown_transport(self):
        database = {"functions": {"beta": {"state": "known", "prototype": "extern int beta(int);"}}}
        output = declarations(
            self.project, self.policy, "us", "jal beta\nnop\n", "", function="alpha", database=database
        )
        self.assertIn("extern int beta(int);", output)
