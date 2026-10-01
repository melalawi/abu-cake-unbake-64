"""Explicit callee ABI types override per-call register heuristics."""

import os
import tempfile
import unittest
from pathlib import Path
from typing import cast

from tests.decomp.support import fixture
from unbake.decomp.draft_fp import register_pairs
from unbake.decomp.draft_signatures import declarations
from unbake.project.config import Policy


class DraftSignatureTests(unittest.TestCase):
    def test_scalar_and_private_pointer_definitions_use_selected_version(self) -> None:
        with tempfile.TemporaryDirectory(dir=os.environ["TMPDIR"]) as temporary:
            project, policy, _ = fixture(Path(temporary))
            source = project.src / "beta.c"
            source.write_text(
                '#include "types.h"\ntypedef struct { s32 x; } Private;\n'
                "#if NON_MATCHING\nfloat beta(Private *value, float scale) { return value->x * scale; }\n"
                "#else\ns32 beta(void) { return 0; }\n#endif\n"
            )
            result = declarations(project, cast(Policy, policy), "us", "jal beta\nnop\n", "")
            self.assertEqual(result, "float beta(void *, float);")
            self.assertEqual(declarations(project, cast(Policy, policy), "us", "jal beta\n", result), "")

    def test_independent_saved_fprs_get_disjoint_pairs_without_a_capacity_limit(self) -> None:
        text = "\n".join(f"ldc1 $f{number}, {number * 8}($sp)" for number in range(20, 32))
        result = register_pairs(text, ("-mfp64",), "alpha")
        import re

        numbers = [int(value) for value in re.findall(r"\$f(\d+)", result)]
        self.assertEqual(len(set(numbers)), 12)
        self.assertTrue(all(number % 2 == 0 for number in numbers))
        self.assertTrue(all(number >= 20 for number in numbers))
        self.assertEqual(register_pairs(text, (), "alpha"), text)
