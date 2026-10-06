"""An FP temporary at an integer-returning epilogue is not a declared result ABI."""

import unittest

from tests.typemap.test_solver import facts, solve
from unbake.typemap.evidence import abi

# li v0,1; add.s f0,f12,f14; jr ra; nop. Both registers have values;
# without a declaration or a consuming caller, neither is identified as the result.
LEAF = (0x80003000, [0x24020001, 0x460E6000, 0x03E00008, 0])


class ReturnAmbiguityTests(unittest.TestCase):
    def test_unconsumed_dual_result_does_not_guess_floating_or_integer_signature(self):
        result = abi(facts({"leaf": LEAF})["functions"])["leaf"]
        self.assertEqual(result["defined_returns"], ["r2", "f0"])
        self.assertIsNone(result["return_register"])
        self.assertFalse(result["return_known"])
        self.assertFalse(result["void"])
        record = solve({"leaf": LEAF}, "")["functions"]["leaf"]
        self.assertIsNone(record["prototype"])
        self.assertIsNone(record["abi_declaration"]["prototype"])

    def test_evidenced_consumption_selects_the_register_and_declaration_can_disambiguate(self):
        for register, operation in (("r2", 0x24430001), ("f0", 0x46000086)):
            with self.subTest(register=register):
                programs = {"leaf": LEAF, "caller": (0x80001000, [0x0C000C00, 0, operation, 0x03E00008, 0])}
                record = abi(facts(programs)["functions"])["leaf"]
                self.assertEqual(record["return_register"], register)
                self.assertTrue(record["return_known"])
                declared = abi(facts({"leaf": LEAF})["functions"], {"leaf": register})["leaf"]
                self.assertEqual(declared["return_register"], register)
                self.assertTrue(declared["return_known"])
