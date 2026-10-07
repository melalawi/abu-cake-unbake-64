"""The real BattleTanx stack overlay reaches proof without inventing shared types."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.decomp.support import fixture
from unbake.config import Held
from unbake.decomp import m2c, measured_storage
from unbake.decomp.draft_input import stack_locals
from unbake.decomp.draft_layouts import access_widths, normalize
from unbake.decomp.draft_macros import lower
from unbake.decomp.field_access import share

PAYLOADS = Path(__file__).parents[1] / "fixtures" / "battletanx_measured_storage"
FUNCTION = "func_800949D8_us"


class LocalStorageGateTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(dir=os.environ["TMPDIR"])
        self.addCleanup(temporary.cleanup)
        self.project, self.policy, _ = fixture(Path(temporary.name).resolve(), case=self)
        self.raw = (PAYLOADS / "bt_shared_raw.c").read_text()
        self.context = (PAYLOADS / "bt_shared_context.c").read_text()
        self.assembly = (PAYLOADS / "bt_shared.s").read_text()
        (self.project.include[0] / "payload.h").write_text(self.context)

    def draft(self, source):
        with (
            patch.object(m2c.similar, "retrieve", return_value=[]),
            patch.object(m2c, "assembly_source", return_value=(self.assembly, 0x800949D8)) as assembly,
            patch.object(m2c, "canonical_entry", return_value=self.assembly),
            patch.object(m2c.draft_abi, "declarations", return_value=""),
            patch.object(m2c, "run_tool", return_value=source) as decompiler,
            patch.object(m2c, "prove") as proof,
        ):
            result = m2c.draft(
                self.project,
                self.policy,
                FUNCTION,
                "us",
                self.project.work,
                self.project.root / "extract/us",
                type_context="/* solved shared context */",
                use_type_db=False,
            )
            self.assertEqual(assembly.call_count, 1)
            self.assertEqual(decompiler.call_count, 1)
            self.assertEqual(proof.call_count, 1)
            self.assertIn("union", proof.call_args.args[4].read_text())
            return result

    def test_saved_raw_stack_overlay_passes_final_gate_and_reaches_proof_once(self):
        output = self.draft(self.raw)
        self.assertIn("union { unsigned char bytes[0x38];", output)
        self.assertIn("m2c_stack.bytes", output)
        self.assertNotIn("M2C_UNK", output)
        self.assertNotIn("M2C_FIELD", output)

    def test_saved_pipeline_recreates_the_recorded_final_payload(self):
        output, header = measured_storage.prepare(self.project, FUNCTION, self.raw, self.assembly)
        self.assertIsNone(header)
        output = normalize(output, self.context, access_widths(self.assembly))
        output = stack_locals(output, self.context, FUNCTION, self.assembly)
        output = lower(output, self.context, allow_fields=True)
        output, _ = share(self.project, FUNCTION, output, self.context)
        output = lower(output, self.context)
        self.assertEqual(output, (PAYLOADS / "bt_shared_final.c").read_text())
        m2c._shared_type_gate(output, FUNCTION)

    def test_global_aggregate_contracts_and_typedefs_still_refuse(self):
        for declaration in (
            "struct Invented { int field; };",
            "union Invented { int field; };",
            "struct { int field; } global;",
            "typedef int Invented;",
            "void external(struct Invented { int field; } *);",
        ):
            with self.subTest(declaration=declaration), self.assertRaisesRegex(Held, "must reuse solved shared types"):
                m2c._shared_type_gate(declaration + "\n" + self.raw, FUNCTION)

    def test_local_bit_transport_and_declaration_words_in_literals_pass(self):
        output = lower("float alpha(int bits) { return M2C_BITWISE(float, bits); }", "")
        m2c._shared_type_gate(output, "alpha")
        m2c._shared_type_gate('const char *description = "typedef union { /* comment */ }"; ' + output, "alpha")
