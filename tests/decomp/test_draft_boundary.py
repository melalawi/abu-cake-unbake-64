"""Draft refusals, measured stack views, and analysis-only delay slots."""

import io
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from tests.decomp.support import fixture
from unbake.decomp import m2c
from unbake.decomp.draft_abi import stack_arguments
from unbake.decomp.draft_asm import delay_slots, local_targets, saved_returns
from unbake.decomp.draft_input import stack_locals
from unbake.decomp.draft_layouts import normalize
from unbake.decomp.draft_macros import lower
from unbake.layout.structs import layouts
from unbake.layout.structs_fold import fold
from unbake.project.config import Held


class DraftBoundaryTests(unittest.TestCase):
    def test_bitwise_call_preserves_bits_and_evaluates_once(self) -> None:
        context = "typedef float f32; typedef int s32; s32 bits(void);"
        source = lower("f32 alpha(void) { return M2C_BITWISE(f32, bits()); }", context)
        self.assertNotIn("M2C_BITWISE", source)
        self.assertEqual(source.count("bits()"), 1)
        self.assertIn("union", source)
        with self.assertRaisesRegex(Held, "requires addressable value"):
            lower("float alpha(void) { return M2C_BITWISE(float, missing()); }", "")

    def test_saved_return_and_measured_local_targets(self) -> None:
        text = "glabel alpha\naddu $s2, $ra, $zero\njal callee\nnop\njr $s2\nnop\n"
        self.assertIn("jr $ra", saved_returns(text))
        overwritten = text.replace("jr $s2", "move $s2, $v0\njr $s2")
        self.assertEqual(saved_returns(overwritten), overwritten)
        text = "glabel alpha\nbnez $a0, .L80001008_auto\nnop\n/* 000048 80001008 03E00008 */ jr $ra\nnop\n"
        self.assertIn(".L80001008_auto:\n", local_targets(text))
        self.assertEqual(local_targets(local_targets(text)), local_targets(text))
        outside = text.replace(".L80001008_auto", ".L80002000_auto")
        self.assertEqual(local_targets(outside), outside)

    def test_lost_stack_operand_requires_agreement_at_every_mapped_call(self) -> None:
        source = "void alpha(int arg0) { beta(M2C_ERROR(/* Unable to find stack arg 0x10 in block */)); }"
        value = {"origins": [{"id": "param:alpha:r4", "offset": 0}], "constant": None}
        call = {"callee": "beta", "function": "alpha", "arguments": {"stack16": value}}
        body = {"calls": [call, call]}
        recovered = stack_arguments(source, "void beta(int);", "alpha", body)
        self.assertIn("beta(arg0)", recovered)
        self.assertIn("types.abi.stack_argument:", recovered)
        divergent = {**call, "arguments": {"stack16": {"origins": [], "constant": 2}}}
        self.assertEqual(stack_arguments(source, "void beta(int);", "alpha", {"calls": [call, divergent]}), source)

    def test_refusals_name_function_and_do_not_announce_invalid_c(self) -> None:
        for output, reason in (
            ("void alpha(void) { M2C_ERROR(/* Read from unset register $a0 */); }", "Read from unset register"),
            ("void alpha(void) { M2C_ERROR(/* mtc0 $a0, $18 */); }", "mtc0"),
            ("void alpha(void) { *(int *)saved_reg_s3 = 1; }", "incoming saved register $s3"),
            ("int alpha(void) { return missing; }", "missing"),
            ("void alpha(void *p) { *p = 1; }", "invalid use of void expression"),
        ):
            with self.subTest(output=output), tempfile.TemporaryDirectory(dir=os.environ["TMPDIR"]) as temporary:
                root = Path(temporary).resolve()
                self.compiler_error = (
                    "missing: undeclared identifier"
                    if "return missing" in output
                    else "invalid use of void expression"
                    if "*p = 1" in output
                    else ""
                )
                project, policy, _ = fixture(root, case=self)
                tool = root / "m2c"
                tool.write_text(f"#!{sys.executable}\nprint({output!r})\n")
                tool.chmod(0o755)
                policy.m2c = tool
                messages = io.StringIO()
                with redirect_stdout(messages), self.assertRaises(Held) as caught:
                    m2c.draft(project, policy, "alpha", "us", project.work)
                self.assertTrue(caught.exception.reason.startswith("alpha:"))
                self.assertIn(reason, caught.exception.reason)
                self.assertNotIn("draft_path:", messages.getvalue())
                self.assertIn("draft alpha", caught.exception.next_action)
                if self.compiler_error:
                    self.assertIn("m2c/type compile proof failed", caught.exception.reason)
                    retained = Path(caught.exception.reason.split("draft_path: ", 1)[1].splitlines()[0])
                    self.assertTrue(retained.is_file())
                    self.assertIn(output, retained.read_text())
                self.assertFalse((project.include[0] / "structs.h").exists())
                self.assertFalse(list((project.work).glob("*/alpha.c")))

    def test_unknown_widths_and_complex_bitwise_lvalues_compile(self) -> None:
        context = "typedef unsigned char u8; typedef int s32; typedef int M2C_UNK32; typedef float f32;"
        text = "M2C_UNK32 alpha(s32 *p) { return M2C_BITWISE(f32, *(p + 1)); }"
        text = lower(normalize(text, context), context)
        self.assertNotIn("M2C_UNK", text)
        self.assertNotIn("M2C_BITWISE", text)

    def test_unknown_pointer_transport_needs_no_pointee_layout(self) -> None:
        source = "void alpha(void) { M2C_UNK *p; p = &opaque; use(p); }"
        normalized = normalize(source, "typedef int s32; // typedef s32 M2C_UNK;\n")
        self.assertIn("void *p", normalized)
        self.assertIn("pointee unknown", normalized)
        self.assertNotIn("M2C_UNK", normalized)
        self.assertNotIn("typedef", normalized)

    def test_string_literals_prove_elements_even_for_pointer_arithmetic(self) -> None:
        source = 'void alpha(void) { M2C_UNK *p; p = "words"; p += 2; use(p); }'
        normalized = normalize(source, "typedef int s32;")
        self.assertIn("char *p", normalized)
        self.assertIn("types.abi.string_pointer", normalized)
        self.assertNotIn("M2C_UNK", normalized)

    def test_opaque_pointer_sized_uses_still_refuse_by_name(self) -> None:
        for use in ("p[0] = 2;", "p += 2;", "use(*p);", "p++;", "use(p->field);"):
            source = "void alpha(void) { M2C_UNK *p; p = &opaque; " + use + " }"
            with self.subTest(use=use), self.assertRaisesRegex(Held, "unknown type has no declared target layout"):
                normalize(source, "typedef int s32;")

    def test_bitwise_fields_are_lowered_after_sharing(self) -> None:
        with tempfile.TemporaryDirectory(dir=os.environ["TMPDIR"]) as temporary:
            root = Path(temporary).resolve()
            project, policy, _ = fixture(root, case=self)
            tool = root / "m2c"
            tool.write_text(
                f"#!{sys.executable}\n"
                "print('float alpha(void *p) { return M2C_BITWISE(float, M2C_FIELD(p, s32 *, 0)); }')\n"
            )
            tool.chmod(0o755)
            policy.m2c = tool
            source = m2c.draft(project, policy, "alpha", "us", project.work)
            self.assertNotIn("M2C_BITWISE", source.read_text())
            self.assertNotIn("M2C_FIELD", source.read_text())
            self.assertNotIn("struct Layout_", source.read_text())
            self.assertIn("->value", source.read_text())

    def test_stack_views_bound_arrays_and_retain_raw_byte_access(self) -> None:
        context = "typedef int s32; typedef unsigned char u8;"
        output = (
            "struct _m2c_stack_alpha {\n /* 0x10 */ u8 sp10[];\n /* 0x20 */ s32 sp20;\n"
            "}; /* size = 0x30 */\nvoid alpha(void) {\n u8 sp10[];\n s32 sp20;\n\n"
            " sp10[2] = 7; sp20 = unksp24; *((s32 *)(sp + 0x20)) = 1;\n}\n"
        )
        fixed = stack_locals(output, context, "alpha", "lw $v0, 0x24($sp)\n")
        self.assertIn("sp10[16]", fixed)
        self.assertIn("m2c_stack.slot_unksp24.unksp24", fixed)
        self.assertIn("m2c_stack.bytes", fixed)

    def test_sdk_gfx_union_word_views_are_preserved(self) -> None:
        with tempfile.TemporaryDirectory(dir=os.environ["TMPDIR"]) as temporary:
            root = Path(temporary).resolve()
            sdk = root / "n64sdk.h"
            original = (
                "typedef unsigned int u32; typedef long long s64;\n"
                "typedef union { struct { u32 w0, w1; } words; s64 align; } Gfx;\n"
            )
            sdk.write_text(original)
            records = layouts("typedef unsigned int u32; struct Gfx { u32 w0, w1; };")
            self.assertEqual(fold(records, root, versions=("us",)), [])
            incompatible = layouts("struct Gfx { float w0, w1; };")
            with self.assertRaisesRegex(Held, "SDK Gfx union"):
                fold(incompatible, root, versions=("us",))
            self.assertEqual(sdk.read_text(), original)

    def test_labeled_slots_keep_both_entry_paths_and_likely_annulment(self) -> None:
        # Tiny interpreter observes registers for taken/not-taken branches
        # and direct entry into the labeled instruction, including branch-likely.
        def execute(text: str, entry: str, taken: bool) -> int:
            labels: dict[str, int] = {}
            instructions = []
            for line in text.splitlines():
                line = line.strip()
                if line.endswith(":"):
                    labels[line[:-1]] = len(instructions)
                elif line:
                    instructions.append(line)
            pc, count = labels[entry], 0
            while pc < len(instructions):
                instruction = instructions[pc]
                if instruction == "inc":
                    count += 1
                elif instruction.startswith(("beqz ", "bnezl ", "b ")):
                    op = instruction.split()[0]
                    target = instruction.split()[-1]
                    jump = op == "b" or taken
                    if (op != "bnezl" or jump) and instructions[pc + 1] == "inc":
                        count += 1
                    pc = labels[target] if jump else pc + 2
                    continue
                pc += 1
            return count

        for op in ("beqz", "bnezl"):
            original = f"entry:\n {op} $v0, done\nslot:\n inc\n inc\ndone:\n nop\n"
            # Use a real mnemonic for preprocessing, then map it for execution.
            prepared = delay_slots(original.replace("inc", "addiu $a0, $a0, 1"), "alpha")
            prepared = prepared.replace("addiu $a0, $a0, 1", "inc")
            for entry in ("entry", "slot"):
                for taken in (False, True):
                    self.assertEqual(execute(original, entry, taken), execute(prepared, entry, taken))
        with self.assertRaisesRegex(Held, "alpha:.*missing its delay slot"):
            delay_slots("glabel alpha\n beqz $v0, tail\n", "alpha")
        fpu = "glabel alpha\n j done\n add.s $f0, $f2, $f4\nalabel done\n jr $ra\n nop\n"
        self.assertEqual(delay_slots(fpu, "alpha"), fpu)
        labeled = "glabel alpha\n jr $ra\nalabel slot\n add.s $f0, $f2, $f4\n"
        self.assertEqual(delay_slots(labeled, "alpha").count("add.s"), 2)
