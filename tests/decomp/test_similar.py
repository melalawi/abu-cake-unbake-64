"""Opcode retrieval, landed-source filtering and draft context regressions."""

import io
import json
import os
import struct
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from typing import cast

from tests.decomp import test_m2c, test_plan
from tests.decomp.support import fixture
from unbake.decomp import m2c, similar
from unbake.config import Held, Host


def words(*values: int) -> bytes:
    return struct.pack(f">{len(values)}I", *values)


class SimilarTests(unittest.TestCase):
    def test_similarity_matches_opcode_sibling_and_excludes_self(self) -> None:
        fixture_test = test_plan.PlanningTests()
        fixture_test.setUp()
        self.addCleanup(fixture_test.doCleanups)
        project = fixture_test.project
        project.src = fixture_test.root / "src"
        project.src.mkdir()
        project.asm = fixture_test.root / "asm"
        (project.src / "sibling.c").write_text("int sibling(void) { return 0; }\n")
        (project.src / "unrelated.c").write_text("int unrelated(void) { return 0; }\n")
        words = (0x27BDFFE0, 0xAFBF001C, 0x00801021, 0x8C820000, 0x24420001, 0x8FBF001C, 0x03E00008, 0x27BD0020)
        target = struct.pack(">8I", *words)
        sibling = struct.pack(">8I", *(word ^ 4 if index not in (2, 6) else word for index, word in enumerate(words)))
        fixture_test.layout(
            [
                ("target", target, "asm", ()),
                ("sibling", sibling, "c", ()),
                ("unmatched", target, "asm", ()),
                ("unrelated", bytes.fromhex("40026000") * 8, "c", ()),
            ]
        )
        rows = similar.retrieve(project, "target", "us", bound=0)
        self.assertEqual([(row.function, row.distance) for row in rows], [("sibling", 0)])
        fixture_test.layout([("target", test_plan.BODY, "asm", ()), ("sibling", sibling, "c", ())])
        self.assertEqual(similar.retrieve(project, "target", "us", bound=0), [])

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
            rows = similar.retrieve(project, "alpha", "us")
            self.assertEqual([row.function for row in rows], ["beta"])
            self.assertEqual(rows[0].distance, 0.0)
            self.assertIn("return 2", rows[0].c)
            self.assertIn("0x24020002", rows[0].asm)
            self.assertIn("glabel beta", rows[0].asm)
            self.assertIn("Assembly:", similar.context(rows))
            self.assertEqual(similar.retrieve(project, "alpha", "us", top_k=1), rows)
            with self.assertRaisesRegex(Held, "expected one function row"):
                similar.retrieve(project, "missing", "us")

    def test_draft_passes_landed_definition_and_assembly_to_context_and_announces_it(self) -> None:
        fixture_test = test_m2c.M2cTests()
        fixture_test.setUp()
        self.addCleanup(fixture_test.doCleanups)
        project = fixture_test.project
        split = project.version("us").split
        split.write_text(split.read_text().replace("asm, beta", "c, beta"))
        (project.src / "beta.c").write_text('#include "types.h"\nint beta(void) { return 2; }\n')
        with redirect_stdout(io.StringIO()) as output:
            source = m2c.draft(project, cast(Host, fixture_test.policy), "alpha", "us", fixture_test.scratch)
        invocation = json.loads((source.parent / "invocation.json").read_text())
        self.assertIn("int beta(void)", invocation["context"])
        self.assertIn("glabel beta", invocation["context"])
        self.assertEqual(invocation["context"].count("typedef int s32"), 1)
        self.assertIn("similar context used: beta (distance=0.000000, edits=0)", output.getvalue())
        self.assertIn("return 2", (source.parent / "similar-context.txt").read_text())
        self.assertNotIn("beta", source.read_text())

    def test_named_refusals_for_missing_and_ambiguous_targets(self) -> None:
        with tempfile.TemporaryDirectory(dir=os.environ["TMPDIR"]) as directory:
            project, _, _ = fixture(Path(directory).resolve(), case=self)
            for function in (None, "", "unknown"):
                with self.subTest(function=function), self.assertRaisesRegex(Held, "function"):
                    similar.retrieve(project, function, "us")
            path = project.version("us").split
            path.write_text(path.read_text().replace(", beta]", ", folder/alpha]"))
            with self.assertRaisesRegex(Held, "expected one function row"):
                similar.retrieve(project, "alpha", "us")
