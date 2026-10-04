"""Machine transport stays separate from unresolved semantic signatures."""

import unittest

from tests.typemap.test_solver import facts, solve
from unbake.typemap.abi_declarations import prototype
from unbake.typemap.evidence import abi
from unbake.typemap.mips import UNKNOWN, Value


class AbiTests(unittest.TestCase):
    def test_published_contract_without_machine_abi_has_no_caller_variant(self) -> None:
        from unbake.typemap.abi_declarations import for_caller

        carrier = {"prototype": None, "reasons": ["types.abi.absent"]}
        record = {"abi": None, "abi_declaration": carrier}
        self.assertEqual(for_caller(record, "published_caller"), carrier)
        self.assertIsNot(for_caller(record, "published_caller"), carrier)

    def test_join_of_distinct_constants_preserves_definedness_without_selecting_a_value(self):
        joined = Value(constant=1).merge(Value(constant=2))
        self.assertTrue(joined.data()["unknown"])
        self.assertTrue(joined.data()["defined"])
        self.assertFalse(joined.merge(UNKNOWN).data()["defined"])

    def test_join_preserves_forwarded_return_dependencies(self):
        origin = "return:caller:us:2:r2"
        joined = Value(((origin, 0),), dependencies=(origin,)).merge(Value(constant=2))
        self.assertTrue(joined.data()["unknown"])
        self.assertEqual(joined.data()["dependencies"], [origin])

    def test_defined_argument_join_does_not_require_one_known_constant(self):
        mapped = facts(
            {
                "caller": (
                    0x80001000,
                    [0x10800004, 0, 0x24050001, 0x10000002, 0, 0x24050002, 0x0C000800, 0, 0x03E00008, 0],
                ),
                "leaf": (0x80002000, [0x00851021, 0x03E00008, 0]),
            }
        )
        result = abi(mapped)["leaf"]
        self.assertEqual(result["registers"], ["r4", "r5"])
        self.assertTrue(result["arity_known"])
        self.assertFalse(result["missing"])

    def test_semantic_conflict_emits_an_abi_carrier_without_claiming_a_known_type(self):
        result = solve({"leaf": (0x80001000, [0x8C820000, 0x03E00008, 0])}, "")
        record = result["functions"]["leaf"]
        record.update(state="conflict")
        record["params"][0].update(state="conflict", type=None)
        record["return"].update(state="conflict", type=None)
        carrier = prototype("leaf", record, {})
        self.assertEqual(carrier["prototype"], "int leaf(int);")
        self.assertTrue(any("types.abi.word:" in reason for reason in carrier["reasons"]))
        self.assertIsNone(record["params"][0]["type"])
        self.assertIsNone(record["return"]["type"])
        self.assertEqual(record["state"], "conflict")

    def test_existing_declaration_survives_unrelated_value_flow_conflicts(self):
        result = solve(
            {"caller": (0x80001000, [0x0C000800, 0, 0x03E00008, 0]), "leaf": (0x80002000, [0x03E00008, 0])},
            "void caller(int *p); void leaf(float *p);",
        )
        record = result["functions"]["caller"]
        self.assertEqual(record["state"], "conflict")
        self.assertEqual(record["abi_declaration"]["prototype"], "void caller(int *p);")
        self.assertIn("types.abi.declared", record["abi_declaration"]["reasons"][0])

    def test_distant_unproved_stack_slot_leaves_arguments_unspecified(self):
        registers = ["r4", "stack4294967292"]
        record = {
            "abi": {
                "registers": registers,
                "argument_slots": ["r4"],
                "missing": [],
                "conflicts": [],
                "used_returns": [],
                "call_sites": 2,
                "return_known": True,
                "void": True,
            },
            "params": [{"register": reg, "state": "unknown", "type": None} for reg in registers],
            "return": {"type": "void"},
        }
        carrier = prototype("leaf", record, {})
        self.assertEqual(carrier["prototype"], "void leaf();")
        self.assertFalse(carrier["parameters_known"])
        self.assertTrue(any("types.abi.slots" in reason for reason in carrier["reasons"]))
        self.assertEqual(record["abi"]["registers"], registers)
        self.assertTrue(all(param["type"] is None for param in record["params"]))

    def test_unused_slot_requires_evidence_from_every_call_site(self):
        record = {
            "abi": {
                "registers": ["r4", "r6"],
                "argument_slots": ["r4", "r5", "r6"],
                "missing": [],
                "conflicts": [],
                "used_returns": [],
                "call_sites": 2,
                "return_known": True,
                "void": True,
            },
            "params": [{"register": reg, "state": "unknown", "type": None} for reg in ("r4", "r6")],
            "return": {"type": "void"},
        }
        carrier = prototype("leaf", record, {})
        self.assertEqual(carrier["prototype"], "void leaf(int, int, int);")
        self.assertTrue(any("unused_slot" in reason for reason in carrier["reasons"]))
        record["abi"]["argument_slots"].remove("r5")
        unresolved = prototype("leaf", record, {})
        self.assertEqual(unresolved["prototype"], "void leaf();")
        self.assertFalse(unresolved["parameters_known"])

    def test_proven_return_survives_callers_that_ignore_the_value(self):
        record = {
            "abi": {
                "registers": [],
                "missing": [],
                "conflicts": [],
                "used_returns": [],
                "call_sites": 1,
                "return_register": "r2",
                "return_known": True,
                "void": False,
            },
            "params": [],
            "return": {"state": "known", "type": "int"},
        }
        self.assertEqual(prototype("leaf", record, {})["prototype"], "int leaf(void);")
        record["return"].update(state="unknown", type=None)
        self.assertEqual(prototype("leaf", record, {})["prototype"], "void leaf(void);")

    def test_declared_wrapper_epilogue_propagates_return_demand(self):
        mapped = facts(
            {
                "caller": (0x80001000, [0x0C000800, 0, 0x03E00008, 0]),
                "leaf": (0x80002000, [0x24020001, 0x03E00008, 0]),
            }
        )
        result = abi(mapped, {"caller": "r2"})["leaf"]
        self.assertEqual(result["used_returns"], ["r2"])
        self.assertEqual(result["return_register"], "r2")
        self.assertTrue(result["return_known"])

    def test_cpu_control_word_result_proves_forwarded_word_abi(self):
        mapped = facts(
            {
                "caller": (0x80001000, [0x0C000800, 0, 0x30420001, 0x03E00008, 0]),
                "leaf": (0x80002000, [0x40026000, 0x03E00008, 0]),
            }
        )
        result = abi(mapped, {"caller": "r2"})
        self.assertTrue(result["leaf"]["return_known"])
        self.assertTrue(result["caller"]["return_known"])
        self.assertEqual(result["caller"]["defined_returns"], ["r2"])

    def test_forwarding_requires_result_at_every_callee_exit(self):
        for middle in ([], [0x00441021]):
            mapped = facts(
                {
                    "caller": (0x80001000, [0x0C000800, 0, *middle, 0x03E00008, 0]),
                    "leaf": (0x80002000, [0x10800004, 0, 0x24020001, 0x03E00008, 0, 0x03E00008, 0]),
                }
            )
            with self.subTest(arithmetic=bool(middle)):
                result = abi(mapped, {"caller": "r2"})
                self.assertFalse(result["leaf"]["return_known"])
                self.assertFalse(result["caller"]["return_known"])
                self.assertNotIn("r2", result["caller"]["defined_returns"])

    def test_word_transport_does_not_promise_initialized_forwarded_value(self):
        result = solve(
            {
                "caller": (0x80001000, [0x00808021, 0x0C000800, 0, 0x00501021, 0x03E00008, 0]),
                "leaf": (0x80002000, [0x10800004, 0, 0x24020001, 0x03E00008, 0, 0x03E00008, 0]),
            },
            "",
        )
        record = result["functions"]["caller"]
        self.assertNotEqual(record["state"], "known")
        self.assertIsNone(record["prototype"])
        carrier = record["abi_declaration"]
        self.assertEqual(carrier["prototype"], "int caller(int);")
        self.assertTrue(any("types.abi.result_value_unknown:" in reason for reason in carrier["reasons"]))

    def test_array_carriers_use_valid_unnamed_c_declarators(self):
        from unbake.typemap.declarations import extract

        record = {
            "abi": {
                "registers": ["r4"],
                "missing": [],
                "conflicts": [],
                "used_returns": [],
                "call_sites": 1,
                "return_known": True,
                "void": True,
            },
            "params": [{"register": "r4", "state": "known", "type": "unsigned char[4]"}],
            "return": {"type": "void"},
        }
        carrier = prototype("leaf", record, {})
        self.assertEqual(carrier["prototype"], "void leaf(unsigned char [4]);")
        extract(carrier["prototype"], {})

    def test_tail_return_must_agree_with_direct_exits(self):
        mapped = facts(
            {
                "caller": (0x80001000, [0x10800004, 0, 0x24020001, 0x03E00008, 0, 0x08000800, 0]),
                "leaf": (0x80002000, [0x03E00008, 0]),
            }
        )
        result = abi(mapped)["caller"]
        self.assertFalse(result["return_known"])
        self.assertIn("direct exits and tail calls disagree on return ABI", result["conflicts"])

    def test_tail_cycle_without_evidenced_exit_stays_unknown(self):
        mapped = facts(
            {
                "caller": (0x80001000, [0x08000800, 0]),
                "leaf": (0x80002000, [0x08000400, 0]),
            }
        )
        for result in abi(mapped).values():
            self.assertFalse(result["return_known"])
            self.assertFalse(result["void"])

    def test_big_endian_subword_stack_input_occupies_its_argument_word(self):
        mapped = facts({"leaf": (0x80001000, [0x27BDFFE0, 0x97A20032, 0x27BD0020, 0x03E00008, 0])})
        result = abi(mapped)["leaf"]
        self.assertEqual(result["registers"], ["stack16"])
        self.assertNotIn("stack18", result["registers"])

    def test_conflicting_return_contracts_narrow_only_for_the_consuming_caller(self):
        from unbake.typemap.abi_declarations import for_caller

        result = solve(
            {
                "integer": (0x80001000, [0x0C000C00, 0, 0x24430001, 0x03E00008, 0]),
                "floating": (0x80002000, [0x0C000C00, 0, 0x46000086, 0x03E00008, 0]),
                "leaf": (0x80003000, [0x24020001, 0x460E6000, 0x03E00008, 0]),
            },
            "",
        )
        record = result["functions"]["leaf"]
        self.assertEqual(record["state"], "conflict")
        self.assertIsNone(record["prototype"])
        self.assertIsNone(record["abi_declaration"]["prototype"])
        self.assertEqual(record["abi"]["defined_returns"], ["r2", "f0"])
        integer = for_caller(record, "integer")
        floating = for_caller(record, "floating")
        self.assertEqual(integer["prototype"], "int leaf(float, float);")
        self.assertEqual(floating["prototype"], "float leaf(float, float);")
        self.assertTrue(any("types.abi.caller_contract" in reason for reason in integer["reasons"]))
        self.assertIsNone(for_caller(record, "other")["prototype"])
        self.assertEqual(record["state"], "conflict")

    def test_incompatible_produced_return_registers_remain_unknown(self):
        record = {
            "abi": {
                "registers": [],
                "missing": [],
                "conflicts": [],
                "used_returns": ["r2", "f0"],
                "return_known": True,
            },
            "params": [],
            "return": {"type": None},
        }
        carrier = prototype("leaf", record, {})
        self.assertIsNone(carrier["prototype"])
        self.assertIn("types.abi.return", carrier["reasons"][0])

    def test_caller_ignoring_unresolved_fp_return_has_void_call_contract(self):
        from unbake.typemap.abi_declarations import for_caller

        result = solve(
            {
                "floating": (0x80001000, [0x0C000C00, 0, 0x46000086, 0x03E00008, 0]),
                "discarding": (0x80002000, [0x0C000C00, 0, 0x03E00008, 0]),
                "leaf": (0x80003000, [0x00851021, 0x44820000, 0x03E00008, 0]),
            },
            "",
        )
        record = result["functions"]["leaf"]
        self.assertIsNone(record["prototype"])
        self.assertIsNone(record["abi_declaration"]["prototype"])
        self.assertIsNone(for_caller(record, "floating")["prototype"])
        self.assertEqual(for_caller(record, "discarding")["prototype"], "void leaf(int, int);")
        self.assertIsNone(for_caller(record, "absent")["prototype"])

    def test_unwritten_floating_register_is_not_a_produced_return(self):
        mapped = facts(
            {
                "integer": (0x80001000, [0x0C000C00, 0, 0x00401821, 0x03E00008, 0]),
                "floating": (0x80002000, [0x0C000C00, 0, 0x46000086, 0x03E00008, 0]),
                "leaf": (0x80003000, [0x24020001, 0x03E00008, 0]),
            }
        )
        result = abi(mapped)["leaf"]
        self.assertEqual(result["return_register"], "r2")
        self.assertEqual(result["unproven_return_reads"], ["f0"])
        self.assertTrue(result["return_known"])
        self.assertTrue(result["conflicts"])
