"""Evidence must distinguish a closed boundary from merely plausible code."""

import json
import struct
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from unbake.layout import boundary, boundary_signatures
from unbake.layout.boundary_signatures import Signature
from unbake.layout.split_create import complete_executable, loaded_rows
from unbake.project.config import Held


class BoundaryTests(unittest.TestCase):
    def test_offline_signature_seeds_uncalled_library_before_heuristics(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "sdk.json"
            path.write_text(
                json.dumps(
                    {
                        "source": "SDK object fixture",
                        "signatures": [{"name": "sdk_leaf", "words": ["03e00008", "24020001"], "masks": ["0", "0"]}],
                    }
                )
            )
            data = struct.pack(">4I", 0x03E00008, 0, 0x03E00008, 0x24020001)
            with patch.dict("os.environ", {"UNBAKE_BOUNDARY_SIGNATURES": str(path)}):
                self.assertIn("[0x8, asm]", loaded_rows(data, 0, len(data), 0x80000000))
            with patch.dict("os.environ", {}, clear=True), self.assertRaisesRegex(Held, "UNBAKE_BOUNDARY_SIGNATURES"):
                boundary_signatures.configured()

    def test_relocation_mask_preserves_nonrelocation_instruction_bits(self) -> None:
        signature = Signature("sdk", "SDK fixture", (0x0C000001, 0x03E00008), (0x03FFFFFF, 0))
        data = struct.pack(">2I", 0x0C123456, 0x03E00008)
        self.assertEqual(list(boundary_signatures.matches(data, 0, 8, (signature,))), [0])
        data = struct.pack(">2I", 0x08123456, 0x03E00008)
        self.assertFalse(boundary_signatures.matches(data, 0, 8, (signature,)))

    def test_tail_call_and_merge_have_different_evidence(self) -> None:
        words = {0: 0x08000004, 4: 0}
        proof = boundary.evidence(words, 0, 8, 0x80000000, {"jal-target"}, {0, 16}, 4)
        self.assertTrue(proof.proven)
        self.assertIn("tail-call:0x80000010", proof.tags)
        proof = boundary.evidence(words, 0, 8, 0x80000000, {"jal-target"}, {0}, 4)
        self.assertFalse(proof.proven)
        self.assertIn("merge-or-unresolved-tail:0x80000010", proof.unproven)

    def test_alignment_is_owned_but_internal_island_is_unproven(self) -> None:
        words = {0: 0x03E00008, 4: 0, 8: 0, 12: 0}
        proof = boundary.evidence(words, 0, 16, 0x80000000, {"boot-entry"}, {0}, 16)
        self.assertTrue(proof.proven)
        self.assertIn("alignment-padding:8", proof.tags)
        words[8] = 0xFFFFFFFF
        proof = boundary.evidence(words, 0, 16, 0x80000000, {"boot-entry"}, {0}, 16)
        self.assertFalse(proof.proven)

    def test_jump_table_requires_ownership_instead_of_becoming_return(self) -> None:
        proof = boundary.evidence({0: 0x00400008, 4: 0}, 0, 8, 0x80000000, {"jal-target"}, {0}, 4)
        self.assertIn("unresolved-indirect-jump-table-ownership", proof.unproven)

    def test_compiler_shape_is_not_an_independent_entry_source(self) -> None:
        words = {0: 0x27BDFFF0, 4: 0xAFBF000C, 8: 0x03E00008, 12: 0x27BD0010}
        proof = boundary.evidence(words, 0, 16, 0x80000000, set(), {0}, 4)
        self.assertIn("compiler-stack-prologue", proof.tags)
        self.assertIn("entry-source-missing", proof.unproven)

    def test_identical_version_bytes_get_agreement_without_overriding_missing_evidence(self) -> None:
        from types import SimpleNamespace
        from typing import cast

        from unbake.layout import boundary_proof, split
        from unbake.project.config import Project

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            rom = root / "rom"
            rom.write_bytes(struct.pack(">2I", 0x03E00008, 0))
            layout = root / "split.yaml"
            layout.write_text(
                "segments:\n  - name: main\n    type: code\n    start: 0x0\n    vram: 0x80000000\n"
                "    subsegments:\n      - [0x0, asm, leaf]\n  - [0x8]\n"
            )
            project = cast(
                Project,
                SimpleNamespace(
                    asm=root / "asm", versions=("a", "b"), version=lambda _: SimpleNamespace(baserom=rom, split=layout)
                ),
            )
            with (
                patch.object(boundary_proof, "audit", return_value=([], [])),
                patch.object(split, "extracted_text", return_value=split.ExtractedText([], ())) as measured,
            ):
                result = boundary_proof.report(project, signatures=None)
            self.assertIn("identical-bytes-boundary-agreement:a,b", result["a"][0].tags)
            self.assertFalse(result["a"][0].proven)
            self.assertEqual(measured.call_args.args[2], [root / "asm/b/leaf.s"])

    def test_signature_seeding_preserves_existing_row_intervals(self) -> None:
        data = struct.pack(">4I", 0x03E00008, 0, 0x03E00008, 0x24020001)
        text = (
            "segments:\n  - name: main\n    type: code\n    start: 0x0\n"
            "    vram: 0x80000000\n    subsegments:\n"
            "      - [0x0, asm]\n      - [0x8, asm]\n  - [0x10, bin]\n"
        )
        with (
            patch.dict("os.environ", {"UNBAKE_BOUNDARY_SIGNATURES": "configured"}),
            patch("unbake.layout.split_analysis.copied_text", return_value=[]),
            patch("unbake.layout.split_analysis.loaded_bounds", return_value=(0x80000010, 0x80000020)),
            patch("unbake.layout.split_analysis.executable_end", return_value=16),
            patch.object(
                boundary_signatures,
                "configured",
                return_value=(Signature("sdk_leaf", "SDK fixture", (0x03E00008, 0x24020001), (0, 0)),),
            ),
        ):
            # A measured boundary must extend the generated code interval.
            text = text.replace("0x10, bin", "0xC, bin")
            result = complete_executable(text, data)
        self.assertEqual(result.count("[0x8, asm]"), 1)

    def test_audit_keeps_reachable_global_jump_label_inside_function(self) -> None:
        from types import SimpleNamespace
        from typing import cast

        from unbake.layout.split_audit import audit
        from unbake.project.config import Project

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            asm = root / "asm/us"
            asm.mkdir(parents=True)
            (asm / "entry.s").write_text(
                ".section .text\nglabel entry\n"
                "/* 10 80000010 08000006 */ j inner\n/* 14 80000014 00000000 */ nop\n"
                "glabel inner\n/* 18 80000018 03E00008 */ jr $ra\n/* 1C 8000001C 00000000 */ nop\n"
            )
            layout = root / "split.yaml"
            layout.write_text(
                "segments:\n  - name: main\n    type: code\n    start: 0x10\n"
                "    vram: 0x80000010\n    subsegments:\n      - [0x10, asm, entry]\n  - [0x20]\n"
            )
            symbols = root / "symbols.txt"
            symbols.write_text("inner = 0x80000018;\n")
            project = cast(
                Project,
                SimpleNamespace(asm=root / "asm", version=lambda _: SimpleNamespace(split=layout, symbols=symbols)),
            )
            findings, edits = audit(project, "us", signatures=())
            self.assertEqual(findings, [])
            self.assertEqual(edits, [])

    def test_invalid_delay_word_cannot_prove_closed_return(self) -> None:
        proof = boundary.evidence({0: 0x03E00008, 4: 0x00000001}, 0, 8, 0x80000000, {"jal-target"}, {0}, 4)
        self.assertFalse(proof.proven)
        self.assertIn("invalid-delay-instruction:0x80000004", proof.unproven)
