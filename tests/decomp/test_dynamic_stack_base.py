"""Real indexed stack byte stores use the measured local frame without new layouts."""

import json
import os
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from pycparser import c_parser  # type: ignore[import-untyped]

from tests.decomp.support import fixture
from unbake import cdecl
from unbake.config import Held
from unbake.decomp import checks, m2c, measured_storage
from unbake.decomp.draft_input import stack_locals
from unbake.decomp.draft_layouts import access_widths, normalize
from unbake.decomp.draft_macros import lower
from unbake.decomp.field_access import share
from unbake.typemap.declarations import clean

PAYLOADS = Path(__file__).parents[1] / "fixtures" / "dynamic_stack_base"
FUNCTION = "func_80115084_us"


def loop_slice(raw):
    """Keep real declarations, loop, address transport and return verbatim."""
    declarations = raw[: raw.index("    sp1C = 0;")]
    loop = raw[raw.index("    sp20 = 0;") : raw.index("    sp48 = sp4C;")]
    call = re.search(r"^\s*sp1C = func_801152E8_us[^\n]+", raw, re.M)[0].lstrip()
    return declarations + loop + "    " + call + "\n    return sp1C;\n}\n"


def raw_bases(source):
    return sum(token[0] == "sp" for token in cdecl.SOURCE_TOKEN.finditer(source))


class DynamicStackBaseTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(dir=os.environ["TMPDIR"])
        self.addCleanup(temporary.cleanup)
        self.project, self.policy, _ = fixture(Path(temporary.name).resolve(), case=self)
        self.raw = (PAYLOADS / "raw.c").read_text()
        self.context = (PAYLOADS / "context.c").read_text()
        self.assembly = (PAYLOADS / "loop.s").read_text()
        self.loop = loop_slice(self.raw)

    def prepare(self, source):
        tokens = Mock(wraps=cdecl.SOURCE_TOKEN)
        with (
            patch.object(measured_storage, "SOURCE_TOKEN", tokens),
            patch.object(measured_storage.atomic_files, "text", wraps=measured_storage.atomic_files.text) as write,
        ):
            output, header = measured_storage.prepare(self.project, FUNCTION, source, self.assembly)
        self.assertEqual(write.call_count, 1)
        self.assertEqual(tokens.sub.call_count, 1)
        self.assertEqual(header.read_text(), (PAYLOADS / "measured.h").read_text())
        self.assertEqual(header.read_text().count("} slot_"), 7)
        return output, header

    def lower(self, source):
        output, header = self.prepare(source)
        context = self.context + header.read_text()
        output = normalize(output, context, access_widths(self.assembly))
        output = stack_locals(output, context, FUNCTION, self.assembly)
        output = lower(output, context, allow_fields=True)
        output, fields = share(self.project, FUNCTION, output, context)
        context += fields.read_text()
        output = lower(output, context)
        return output, context, fields

    def test_real_loop_binds_one_base_and_retains_measured_offsets_and_abi(self):
        self.assertEqual(raw_bases(self.loop), 1)
        output, header = self.prepare(self.loop)
        self.assertEqual(raw_bases(output), 0)
        self.assertEqual(output.count("frame.storage.bytes"), 1)
        self.assertEqual(output.count("struct MeasuredStack_" + FUNCTION + " frame;"), 1)
        self.assertIn("M2C_FIELD((frame.storage.bytes + frame.storage.slot_sp20.sp20), s8 *, 0x24) = 0;", output)
        self.assertIn("temp_t4 = frame.storage.slot_sp20.sp20 + 1;", output)
        self.assertIn("while (temp_t4 < 0x20);", output)
        signature = re.search(r"s32 " + FUNCTION + r"\([^{}]*\)", self.loop)[0]
        self.assertIn(signature, output)
        self.assertIn(
            "func_801152E8_us(arg0, frame.storage.slot_sp44.sp44, (s32) &frame.storage.slot_sp24.sp24, arg4);",
            output,
        )
        self.assertIn("return frame.storage.slot_sp1C.sp1C;", output)
        evidence = json.loads((PAYLOADS / "provenance.json").read_text())
        self.assertIn(f"bytes[{evidence['frame_bytes']}];", header.read_text())
        self.assertIn("padding[36]; unsigned char sp24[32];", header.read_text())
        self.assertEqual(len(re.findall(r"\bsb\s+", self.assembly)), evidence["indexed_store_instructions"])
        self.assertIn("addu       $t1, $sp, $t2", self.assembly)
        self.assertIn("sb         $zero, 0x24($t1)", self.assembly)

    def test_real_loop_survives_field_lowering_and_shared_type_gate(self):
        output, context, fields = self.lower(self.loop)
        self.assertEqual(raw_bases(output), 0)
        self.assertEqual(output.count("frame.storage.bytes + frame.storage.slot_sp20.sp20"), 1)
        self.assertIn("unsigned char padding[36]; signed char value;", fields.read_text())
        self.assertIn("&frame.storage.slot_sp24.sp24", output)
        self.assertFalse(checks.run(output))
        m2c._shared_type_gate(output, FUNCTION)
        c_parser.CParser().parse(clean(context + output))

    def test_public_draft_reaches_proof_once_with_bound_stack_address(self):
        (self.project.include[0] / "payload.h").write_text(self.context)
        with (
            patch.object(m2c.similar, "retrieve", return_value=[]),
            patch.object(m2c, "assembly_source", return_value=(self.assembly, 0x80115084)) as assembly,
            patch.object(m2c, "canonical_entry", return_value=self.assembly),
            patch.object(m2c.draft_abi, "declarations", return_value=""),
            patch.object(m2c, "run_tool", return_value=self.loop) as decompiler,
            patch.object(m2c, "prove") as proof,
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
        self.assertEqual(proof.call_count, 1)
        self.assertEqual(proof.call_args.args[4].read_text(), output)
        self.assertEqual(raw_bases(output), 0)
        self.assertEqual(output.count("frame.storage.bytes + frame.storage.slot_sp20.sp20"), 1)

    def test_full_real_body_requires_its_own_memory_measurements(self):
        with self.assertRaisesRegex(Held, "memory load lacks a unique measured view"):
            self.prepare(self.raw)

    def test_rebinding_is_idempotent_and_preserves_literal_and_comment_tokens(self):
        source = self.loop.replace("    sp20 = 0;", '    use("sp sp24"); /* sp sp20 */\n    sp20 = 0;')
        output, _ = self.prepare(source)
        self.assertIn('use("sp sp24"); /* sp sp20 */', output)
        self.assertEqual(raw_bases(output), 0)
        with patch.object(measured_storage.atomic_files, "text") as write:
            again, header = measured_storage.prepare(self.project, FUNCTION, output, self.assembly)
        self.assertEqual(again, output)
        self.assertIsNone(header)
        self.assertEqual(write.call_count, 0)

    def test_dynamic_index_is_evaluated_once_without_scaling_or_bound_changes(self):
        source = self.loop.replace("(sp + sp20)", "(sp + next_index())")
        output, _ = self.prepare(source)
        self.assertEqual(output.count("next_index()"), 1)
        self.assertIn("M2C_FIELD((frame.storage.bytes + next_index()), s8 *, 0x24) = 0;", output)

    def test_absent_or_unsupported_frame_does_not_invent_storage(self):
        template_end = self.loop.index("s32 " + FUNCTION)
        source = self.loop[template_end:]
        with patch.object(measured_storage.atomic_files, "text") as write:
            output, header = measured_storage.prepare(self.project, FUNCTION, source, self.assembly)
        self.assertEqual(output, source)
        self.assertIsNone(header)
        self.assertEqual(write.call_count, 0)
        self.assertEqual(raw_bases(output), 1)
        for source, reason in (
            (self.loop.replace("/* 0x24 */ char pad24[0x20];", ""), "lacks a measured extent"),
            (self.loop.replace("size = 0x50", "size = 0x40"), "unsupported measured stack interval"),
        ):
            with self.subTest(reason=reason), patch.object(measured_storage.atomic_files, "text") as write:
                with self.assertRaisesRegex(Held, reason):
                    measured_storage.prepare(self.project, FUNCTION, source, self.assembly)
                self.assertEqual(write.call_count, 0)

    def test_global_invented_layout_and_ambiguous_data_still_refuse(self):
        with self.assertRaisesRegex(Held, "must reuse solved shared types"):
            m2c._shared_type_gate("struct Invented { int value; };\n" + self.loop, FUNCTION)
        with self.assertRaisesRegex(Held, "data load lacks a measured width"):
            measured_storage.prepare(
                self.project, FUNCTION, "extern M2C_UNK data; int f(int i) { return *(&data + i); }", self.assembly
            )
