"""Opcode retrieval, landed-source filtering and draft context regressions."""

import os
import struct
import tempfile
import unittest
from pathlib import Path

from tests.decomp.support import fixture
from unbake.config import Held
from unbake.decomp import similar


def words(*values: int) -> bytes:
    return struct.pack(f">{len(values)}I", *values)


class SimilarTests(unittest.TestCase):
    def test_opcodes_ignore_operands_but_preserve_branch_and_float_operations(self) -> None:
        self.assertEqual(similar.opcodes(words(0x24020001)), similar.opcodes(words(0x2403FFFF)))
        self.assertNotEqual(similar.opcodes(words(0x04000000)), similar.opcodes(words(0x04010000)))
        self.assertNotEqual(similar.opcodes(words(0x46000000)), similar.opcodes(words(0x46000001)))
        with self.assertRaises(Held):
            similar.opcodes(b"bad")

    def test_bounded_distance_handles_insertions_deletions_and_cutoff(self) -> None:
        for left, right, expected in (((), (), 0), ((1, 2), (1, 3), 1), ((1,), (1, 2, 3), 2)):
            with self.subTest(left=left, right=right):
                self.assertEqual(similar.levenshtein(left, right, 3), expected)
                self.assertEqual(similar.levenshtein(right, left, 3), expected)
        self.assertEqual(similar.levenshtein((1, 2, 3), (4, 5, 6), 1), 2)
        self.assertEqual(similar.overlap((1, 2), (1, 2)), 1.0)

    def test_retrieval_uses_only_landed_c_and_returns_ranked_source_and_assembly(self) -> None:
        with tempfile.TemporaryDirectory(dir=os.environ["TMPDIR"]) as directory:
            project, _, _ = fixture(Path(directory).resolve(), case=self)
            split = project.version("us").split
            split.write_text(split.read_text().replace("asm, beta", "c, beta").replace("asm, gamma", "c, gamma"))
            (project.src / "beta.c").write_text("int beta(void) { return 2; }\n")
            (project.src / "gamma.c").write_text("#ifdef NON_MATCHING\nint gamma(void) { return 3; }\n#endif\n")
            rows = similar.retrieve(project, "alpha", "us", project.root / "extract/us")
            self.assertEqual([row.function for row in rows], ["beta"])
            self.assertEqual(rows[0].distance, 0.0)
            self.assertIn("return 2", rows[0].c)
            self.assertIn("0x24020002", rows[0].asm)
            self.assertIn("glabel beta", rows[0].asm)
            self.assertIn("Assembly:", similar.context(rows))
            self.assertEqual(similar.retrieve(project, "alpha", "us", project.root / "extract/us", top_k=1), rows)
            with self.assertRaisesRegex(Held, "expected one function row"):
                similar.retrieve(project, "missing", "us", project.root / "extract/us")

    def test_named_refusals_for_missing_and_ambiguous_targets(self) -> None:
        with tempfile.TemporaryDirectory(dir=os.environ["TMPDIR"]) as directory:
            project, _, _ = fixture(Path(directory).resolve(), case=self)
            for function in (None, "", "unknown"):
                with self.subTest(function=function), self.assertRaisesRegex(Held, "function"):
                    similar.retrieve(project, function, "us", project.root / "extract/us")
            path = project.version("us").split
            path.write_text(path.read_text().replace(", beta]", ", folder/alpha]"))
            with self.assertRaisesRegex(Held, "expected one function row"):
                similar.retrieve(project, "alpha", "us", project.root / "extract/us")
