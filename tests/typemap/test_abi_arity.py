"""A machine-derived prototype never says (void) to a call that passes arguments."""

import unittest

from unbake.typemap import abi_declarations

ABI = {
    "registers": [],
    "argument_slots": [],
    "missing": [],
    "conflicts": [],
    "void": True,
    "return_register": None,
    "return_known": True,
    "used_returns": [],
    "call_sites": 1,
}


class ArityTests(unittest.TestCase):
    def test_callers_arguments_keep_the_list_unspecified(self) -> None:
        for label, callers, expected in [
            ("no caller passes one", [], "void func_80293DE8_de(void);"),
            ("a K&R caller passes arg0", ["r4"], "void func_80293DE8_de();"),
        ]:
            with self.subTest(label):
                record = {"abi": {**ABI, "caller_arguments": callers}, "params": [], "return": {"type": "void"}}
                found = abi_declarations.prototype("func_80293DE8_de", record, {})
                self.assertEqual(found["prototype"], expected)
