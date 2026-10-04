"""Explicit callee ABI types override per-call register heuristics."""

import os
import tempfile
import unittest
from pathlib import Path
from typing import cast

from tests.decomp.support import fixture
from unbake.decomp.draft_abi import declarations
from unbake.decomp.draft_fp import register_pairs
from unbake.config import Policy


class DraftSignatureTests(unittest.TestCase):
    def test_scalar_and_private_pointer_definitions_use_selected_version(self) -> None:
        with tempfile.TemporaryDirectory(dir=os.environ["TMPDIR"]) as temporary:
            project, policy, _ = fixture(Path(temporary).resolve(), case=self)
            source = project.src / "beta.c"
            source.write_text(
                '#include "types.h"\ntypedef struct { s32 x; } Private;\n'
                "#if NON_MATCHING\nfloat beta(Private *value, float scale) { return value->x * scale; }\n"
                "#else\ns32 beta(void) { return 0; }\n#endif\n"
            )
            result = declarations(project, cast(Policy, policy), "us", "jal beta\nnop\n", "")
            self.assertEqual(result, "float beta(void *, float);")
            self.assertEqual(declarations(project, cast(Policy, policy), "us", "jal beta\n", result), "")

    def test_database_contract_has_one_owner_when_c_definition_exists(self) -> None:
        with tempfile.TemporaryDirectory(dir=os.environ["TMPDIR"]) as temporary:
            project, policy, _ = fixture(Path(temporary).resolve(), case=self)
            (project.src / "beta.c").write_text("int beta(int value) { return value; }\n")
            database = {
                "functions": {
                    "beta": {
                        "abi_declaration": {
                            "prototype": "int beta(int);",
                            "reasons": ["types.abi.declared: proven C"],
                        }
                    }
                }
            }
            result = declarations(
                project, cast(Policy, policy), "us", "jal beta\nnop\n", "", function="alpha", database=database
            )
            self.assertEqual(result.count("beta("), 1)
            self.assertIn("extern int beta(int);", result)
            self.assertIn("types.abi.declared", result)

    def test_unspecified_callee_uses_complete_word_abi_for_stack_arguments(self) -> None:
        with tempfile.TemporaryDirectory(dir=os.environ["TMPDIR"]) as temporary:
            project, policy, _ = fixture(Path(temporary).resolve(), case=self)
            registers = ["r4", "r5", "r6", "r7", "stack16", "stack20", "stack24", "stack28", "stack32"]
            record = {
                "abi_declaration": {"prototype": "int beta();", "reasons": ["existing unspecified C"]},
                "abi": {"arity_known": True, "registers": registers, "missing": [], "conflicts": []},
            }
            database = {"functions": {"beta": record}}
            result = declarations(
                project,
                cast(Policy, policy),
                "us",
                "jal beta\n",
                "int beta();",
                function="alpha",
                database=database,
            )
            self.assertIn("extern int beta(" + ", ".join(["int"] * 9) + ");", result)
            self.assertIn("types.abi.draft_words", result)
            self.assertEqual(record["abi_declaration"]["prototype"], "int beta();")
            for change in (
                {"arity_known": False},
                {"registers": ["r4", "stack32"]},
                {"registers": ["f12", "r6"]},
                {"missing": ["caller"]},
                {"conflicts": ["input registers differ"]},
            ):
                with self.subTest(change=change):
                    incomplete = {**record, "abi": {**record["abi"], **change}}
                    result = declarations(
                        project,
                        cast(Policy, policy),
                        "us",
                        "jal beta\n",
                        "",
                        function="alpha",
                        database={"functions": {"beta": incomplete}},
                    )
                    self.assertIn("extern int beta();", result)
                    self.assertNotIn("types.abi.draft_words", result)

    def test_independent_saved_fprs_get_disjoint_pairs_without_a_capacity_limit(self) -> None:
        text = "\n".join(f"ldc1 $f{number}, {number * 8}($sp)" for number in range(20, 32))
        result = register_pairs(text, ("-mfp64",), "alpha")
        import re

        numbers = [int(value) for value in re.findall(r"\$f(\d+)", result)]
        self.assertEqual(len(set(numbers)), 12)
        self.assertTrue(all(number % 2 == 0 for number in numbers))
        self.assertTrue(all(number >= 20 for number in numbers))
        self.assertEqual(register_pairs(text, (), "alpha"), text)
