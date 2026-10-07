"""Whole saved body, access-specific views, and real byte-offset expressions."""

import re
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from pycparser import c_parser  # type: ignore[import-untyped]

from tests.decomp import test_dynamic_stack_base as stack_tests
from unbake import cdecl
from unbake.config import Held
from unbake.decomp import m2c, measured_memory, measured_storage
from unbake.typemap.declarations import clean

PAYLOADS = Path(__file__).parents[1] / "fixtures" / "measured_memory"
FUNCTION = "func_80115084_us"


class MeasuredMemoryTests(unittest.TestCase):
    prepare = stack_tests.DynamicStackBaseTests.prepare
    lower = stack_tests.DynamicStackBaseTests.lower

    def setUp(self):
        stack_tests.DynamicStackBaseTests.setUp(self)
        self.assembly = (PAYLOADS / "whole.s").read_text()

    def test_real_full_body_has_fourteen_measured_accesses_and_one_scan(self):
        tokens = Mock(wraps=cdecl.SOURCE_TOKEN)
        with (
            patch.object(measured_memory, "SOURCE_TOKEN", tokens),
            patch.object(measured_memory, "measurements", wraps=measured_memory.measurements) as measure,
            patch.object(measured_memory, "instructions", wraps=measured_memory.instructions) as parse,
        ):
            output, _ = self.prepare(self.raw)
        self.assertEqual(measure.call_count, 1)
        self.assertEqual(parse.call_count, 1)
        self.assertEqual(tokens.finditer.call_count, 1)
        self.assertEqual(output.count("*(("), 14)
        self.assertEqual(output.count("*((unsigned short *)"), 3)
        self.assertEqual(output.count("*((unsigned char *)"), 1)
        self.assertEqual(output.count("*((int *)"), 10)
        self.assertIn("*((unsigned short *)((unsigned char *)arg1 + (frame.storage.slot_sp4C.sp4C * 2))) != 3", output)
        self.assertIn("*((unsigned char *)((unsigned char *)arg1 + (frame.storage.slot_sp44.sp44 * 2))) = arg4", output)
        self.assertIn("*((unsigned short *)((unsigned char *)arg1 + (frame.storage.slot_sp44.sp44 * 2))) = 1", output)
        self.assertIn("*((int *)((unsigned char *)arg5)) = (s32) (*((int *)((unsigned char *)arg5)) + 1)", output)
        self.assertIn("*((int *)((unsigned char *)arg3)) = -1", output)
        self.assertIn("*((int *)((unsigned char *)arg6)) = 0", output)
        signature = re.search(r"s32 " + FUNCTION + r"\([^{}]*\)", self.raw)[0]
        self.assertIn(signature, output)
        self.assertEqual(output.count("func_801152E8_us("), self.raw.count("func_801152E8_us("))
        self.assertEqual(output.count("goto "), self.raw.count("goto "))
        self.assertIn("return frame.storage.slot_sp1C.sp1C;", output)
        self.assertEqual(measured_memory.lower(FUNCTION, output, self.assembly), output)

    def test_whole_body_survives_all_memory_field_and_shared_type_stages(self):
        output, context, _ = self.lower(self.raw)
        self.assertNotIn("M2C_FIELD", output)
        self.assertNotIn("M2C_UNK", output)
        self.assertNotRegex(output[output.index("    struct MeasuredStack_") :], r"\*arg[356]\b|\*\(arg1\b")
        self.assertIn("frame.storage.bytes + frame.storage.slot_sp20.sp20", output)
        self.assertIn("signed char value;", context)
        self.assertIn("unsigned char padding[1];", context)
        m2c._shared_type_gate(output, FUNCTION)
        c_parser.CParser().parse(clean(context + output))

    def test_public_whole_draft_reaches_one_proof_with_all_accesses_lowered(self):
        (self.project.include[0] / "payload.h").write_text(self.context)
        with (
            patch.object(m2c.similar, "retrieve", return_value=[]),
            patch.object(m2c, "assembly_source", return_value=(self.assembly, 0x80115084)) as assembly,
            patch.object(m2c, "canonical_entry", return_value=self.assembly),
            patch.object(m2c.draft_abi, "declarations", return_value=""),
            patch.object(m2c, "run_tool", return_value=self.raw) as decompiler,
            patch.object(m2c, "prove") as proof,
            patch.object(measured_memory, "instructions", wraps=measured_memory.instructions) as parse,
        ):
            output = m2c.draft(
                self.project,
                self.policy,
                FUNCTION,
                "us",
                self.project.work,
                self.project.root / "extract/us",
                type_context=self.context,
                use_type_db=False,
            )
        self.assertEqual(assembly.call_count, 1)
        self.assertEqual(decompiler.call_count, 1)
        self.assertEqual(parse.call_count, 1)
        self.assertEqual(proof.call_count, 1)
        self.assertEqual(proof.call_args.args[4].read_text(), output)
        self.assertEqual(output.count("*(("), 14)
        self.assertNotRegex(output[output.index("    struct MeasuredStack_") :], r"\*arg[356]\b|\*\(arg1\b")
        self.assertNotRegex(output, r"\bsp\b")

    def test_real_negative_offsets_cast_before_subtraction_and_preserve_scalar(self):
        source = (PAYLOADS / "byte_offsets.c").read_text()
        assembly = (PAYLOADS / "byte_offsets.s").read_text()
        with patch.object(measured_memory, "instructions", wraps=measured_memory.instructions) as parse:
            output, header = measured_storage.prepare(self.project, "func_800D1BA0_us", source, assembly)
        self.assertEqual(parse.call_count, 1)
        self.assertIsNone(header)
        self.assertEqual(output.count("(unsigned char *)&D_8037ADCC"), 2)
        self.assertIn("D_8037ADCC + ((unsigned char *)&D_8037ADCC - 0x34)", output)
        self.assertIn("D_8037ADCC + ((unsigned char *)&D_8037ADCC - 0x54)", output)
        self.assertIn("extern s32 D_8037ADCC;", output)
        self.assertEqual(output.count("D_8037ADCC"), source.count("D_8037ADCC"))
        self.assertEqual(measured_memory.lower("func_800D1BA0_us", output, assembly), output)
        c_parser.CParser().parse("typedef int s32;\n" + clean(output))

    def test_byte_offsets_without_corresponding_measurements_hold(self):
        source = (PAYLOADS / "byte_offsets.c").read_text()
        assembly = (PAYLOADS / "byte_offsets.s").read_text().replace("-0x34", "-0x30")
        with self.assertRaisesRegex(Held, "byte address lacks a measured displacement"):
            measured_storage.prepare(self.project, "func_800D1BA0_us", source, assembly)

    def test_missing_overwritten_and_non_affine_argument_evidence_hold_before_writes(self):
        # Mutate the measured argument load/spill/base, retaining the real body.
        for assembly in (
            self.assembly.replace("lhu", "lh").replace("lh ", "ld "),
            self.assembly.replace("sw         $5, 0x54($29)", "sw         $0, 0x54($29)"),
            self.assembly.replace("addu       $12, $9, $11", "sll        $12, $9, 1").replace(
                "addu       $14, $11, $13", "sll        $14, $11, 1"
            ),
        ):
            with self.subTest(assembly=assembly[:40]), patch.object(measured_storage.atomic_files, "text") as write:
                with self.assertRaisesRegex(Held, "memory load lacks a unique measured view"):
                    measured_storage.prepare(self.project, FUNCTION, self.raw, assembly)
                self.assertEqual(write.call_count, 0)

    def test_conflicting_signed_loads_and_stored_values_are_not_order_matched(self):
        assembly = self.assembly.replace("lhu        $15", "lh         $15")
        with self.assertRaisesRegex(Held, "memory load lacks a unique measured view"):
            measured_storage.prepare(self.project, FUNCTION, self.raw, assembly)
        assembly = self.assembly.replace("sb         $24, 0x0($9)", "sh         $24, 0x0($9)\n sb $24, 0x0($9)")
        with self.assertRaisesRegex(Held, "memory store lacks a unique measured view"):
            measured_storage.prepare(self.project, FUNCTION, self.raw, assembly)

    def test_transport_indices_are_evaluated_once_and_tokens_are_preserved(self):
        source = self.raw.replace("(sp4C * 2)", "(next_index() * 2)")
        source = source.replace("    sp1C = 0;", '    use("*arg3 &D_8037ADCC - 0x34"); /* *arg5 */\n    sp1C = 0;')
        output, _ = self.prepare(source)
        self.assertEqual(output.count("next_index()"), 2)
        self.assertIn('use("*arg3 &D_8037ADCC - 0x34"); /* *arg5 */', output)
        self.assertEqual(output.count("*(("), 14)

    def test_multiword_abi_and_unmeasured_compound_store_remain_held(self):
        source = self.raw.replace("s32 arg2", "double arg2")
        # Unsupported argument placement leaves the input intact for its own
        # ABI prerequisite; it does not map later arguments to made-up slots.
        self.assertEqual(measured_memory.lower(FUNCTION, source, self.assembly), source)
        source = self.raw[: self.raw.index("    sp1C = 0;")] + "    *arg5 += 1;\n}\n"
        assembly = self.assembly.replace("sw         $9, 0x0($8)", "sh         $9, 0x0($8)")
        with self.assertRaisesRegex(Held, "read/modify/write lacks a measured width"):
            measured_storage.prepare(self.project, FUNCTION, source, assembly)
