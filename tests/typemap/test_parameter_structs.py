"""A parameter's entry read supports its partial layout independently of a complete prototype."""

import json
import unittest
from pathlib import Path
from types import SimpleNamespace

from unbake.typemap.solver import infer

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "parameter_struct_facts.json"
OWNER = "func_802764D4_de"
BASE = f"param:{OWNER}:r4"


class ParameterStructTests(unittest.TestCase):
    def shape(self, mapped):
        solved = infer(SimpleNamespace(), mapped, [])
        shape = next(s for s in solved["structs"].values() if s["common_base"] == BASE)
        return solved, shape

    def test_real_read_parameter_gets_a_shape_with_unknown_other_argument_and_return(self):
        solved, shape = self.shape(json.loads(FIXTURE.read_text()))
        owner = solved["functions"][OWNER]
        self.assertEqual(next(p for p in owner["params"] if p["register"] == "r5")["state"], "unknown")
        self.assertFalse(owner["abi"]["return_known"])
        self.assertIsNone(owner["prototype"])
        self.assertEqual(shape["state"], "known")
        self.assertTrue(shape["partial"])
        self.assertEqual([f["offset"] for f in shape["fields"]], [0, 4, 8, 12])
        self.assertEqual(solved["nodes"][BASE]["type"], shape["type"] + " *")

    def test_missing_unrelated_argument_does_not_block_the_read_parameter(self):
        mapped = json.loads(FIXTURE.read_text())
        # Keep the real layout evidence and model a second version with one further entry read.
        import copy

        extra = copy.deepcopy(mapped["functions"][OWNER]["versions"]["us-rev1"])
        extra["register_inputs"].append("r7")
        mapped["functions"][OWNER]["versions"]["other"] = extra
        solved, shape = self.shape(mapped)
        self.assertFalse(solved["functions"][OWNER]["abi"]["arity_known"])
        self.assertEqual(shape["state"], "known")

    def test_a_saved_register_is_not_admitted_as_a_parameter(self):
        mapped = json.loads(FIXTURE.read_text().replace(BASE, f"param:{OWNER}:r16"))
        solved = infer(SimpleNamespace(), mapped, [])
        shape = next(s for s in solved["structs"].values() if s["common_base"] == f"param:{OWNER}:r16")
        self.assertEqual(shape["state"], "unknown")
        self.assertIn("not read by its owner", shape["reason"])
