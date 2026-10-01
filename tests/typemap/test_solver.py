"""Signatures seed value-flow constraints; offsets and local names do not."""

import unittest
from types import SimpleNamespace

from unbake.typemap.declarations import canonical, extract, parameter_registers
from unbake.typemap.mips import Analysis
from unbake.typemap.solver import infer


def facts(programs: dict[str, tuple[int, list[int]]]) -> dict:
    targets = {address: name for name, (address, _) in programs.items()}
    return {
        "functions": {
            name: {
                "versions": {
                    "us": {"address": address, **Analysis(name, "us", address, 0x40, words, targets, {}).run()}
                }
            }
            for name, (address, words) in programs.items()
        },
        "globals": {},
    }


def solve(programs: dict, source: str) -> dict:
    return infer(SimpleNamespace(), facts(programs), [extract(source, {"kind": "declared"})])


class SolverTests(unittest.TestCase):
    def test_tagged_typedef_alias_does_not_recursively_expand_its_own_tag(self) -> None:
        self.assertEqual(canonical("Gfx *", {"Gfx": "union Gfx"}), "union Gfx *")

    def test_forwarded_base_requires_independent_users_and_keeps_size_unknown(self) -> None:
        result = solve(
            {
                "caller": (0x80001000, [0x00808021, 0x0C000800, 0x02002021, 0x8E020004, 0x03E00008, 0]),
                "leaf": (0x80002000, [0x8C820000, 0x03E00008, 0]),
            },
            "",
        )
        self.assertEqual(len(result["structs"]), 1)
        shape = next(iter(result["structs"].values()))
        self.assertEqual(shape["common_base"], "param:caller:r4")
        self.assertEqual(shape["users"], ["caller", "leaf"])
        self.assertIsNone(shape["size"])
        self.assertIsNone(shape["declaration"])
        self.assertIsNone(shape["type"])

    def test_several_actual_bases_do_not_establish_one_struct_origin(self) -> None:
        result = solve(
            {
                "caller": (
                    0x80001000,
                    [0x00808021, 0x0C000800, 0x02002021, 0x02002021, 0x0C000800, 0x00C02021, 0x8E020004, 0x03E00008, 0],
                ),
                "leaf": (0x80002000, [0x8C820000, 0x03E00008, 0]),
            },
            "",
        )
        self.assertFalse(result["structs"])

    def test_typed_signature_propagates_to_caller_parameter_and_shared_field(self) -> None:
        result = solve(
            {
                "caller": (0x80001000, [0x00808021, 0x0C000800, 0x02002021, 0x8E020004, 0x03E00008, 0]),
                "leaf": (0x80002000, [0x8C820000, 0x03E00008, 0]),
            },
            "struct Shared { int count; float value; }; int leaf(struct Shared *p);",
        )
        caller = result["functions"]["caller"]
        self.assertEqual(caller["params"][0]["type"], "struct Shared *")
        self.assertIsNone(caller["prototype"])
        self.assertIsNone(caller["arity"])
        self.assertEqual(result["nodes"]["field:param:caller:r4:4"]["type"], "float")
        self.assertEqual(result["structs"]["Shared"]["users"], ["caller", "leaf"])
        self.assertFalse(result["conflicts"])

    def test_equal_offsets_and_parameter_names_do_not_create_a_shared_type(self) -> None:
        result = solve(
            {"first": (0x80001000, [0x8C820004, 0x03E00008, 0]), "second": (0x80002000, [0x8C820004, 0x03E00008, 0])},
            "",
        )
        self.assertFalse(result["structs"])
        self.assertEqual(result["functions"]["first"]["return"]["state"], "unknown")
        self.assertFalse(result["functions"]["first"]["prototype"])

    def test_incompatible_call_types_are_named_and_never_selected_by_confidence(self) -> None:
        result = solve(
            {"caller": (0x80001000, [0x0C000800, 0, 0x03E00008, 0]), "leaf": (0x80002000, [0x03E00008, 0])},
            "void caller(int *p); void leaf(float *p);",
        )
        self.assertTrue(result["conflicts"])
        param = result["functions"]["caller"]["params"][0]
        self.assertEqual(param["state"], "conflict")
        self.assertIsNone(param["type"])
        self.assertEqual(param["alternatives"], ["float *", "int *"])
        self.assertIsNone(result["functions"]["caller"]["prototype"])

    def test_array_extent_only_comes_from_explicit_declaration(self) -> None:
        result = solve({"first": (0x80001000, [0x03E00008, 0])}, "extern float table[12]; extern int unknown[];")
        self.assertEqual(result["arrays"]["table"]["extent"], "12")
        self.assertEqual(result["arrays"]["table"]["state"], "known")
        self.assertIsNone(result["arrays"]["unknown"]["extent"])
        self.assertEqual(result["arrays"]["unknown"]["state"], "unknown")

    def test_o32_float_and_word_alignment_is_explicit(self) -> None:
        def registers(types: list[str]) -> list[str | None]:
            return parameter_registers([{"type": value} for value in types], {})

        self.assertEqual(registers(["float", "double", "int"]), ["f12", "f14", None])
        self.assertEqual(registers(["int", "double"]), ["r4", "r6"])
        self.assertEqual(registers(["double", "int"]), ["f12", "r6"])
        self.assertEqual(registers(["int", "float"]), ["r4", "r5"])

    def test_version_signature_conflict_remains_visible(self) -> None:
        seeds = [extract("int leaf(int *p);", {"version": "us"}), extract("int leaf(float *p);", {"version": "eu"})]
        result = infer(SimpleNamespace(), facts({"leaf": (0x80001000, [0x03E00008, 0])}), seeds)
        self.assertEqual(result["functions"]["leaf"]["state"], "conflict")
        self.assertGreaterEqual(len(result["conflicts"]), 2)


if __name__ == "__main__":
    unittest.main()
