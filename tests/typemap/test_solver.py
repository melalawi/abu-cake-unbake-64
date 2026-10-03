"""Signatures seed value-flow constraints; offsets and local names do not."""

import unittest
from collections import UserDict
from pathlib import Path
from types import SimpleNamespace

from unbake.typemap.declarations import canonical, declarator, extract, parameter_registers
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
    def test_sharded_bodies_are_read_once_during_caller_inference(self) -> None:
        class Reads(UserDict):
            def __getitem__(self, key):
                counts[key] = counts.get(key, 0) + 1
                return super().__getitem__(key)

        counts = {}
        machine = facts(
            {
                "caller": (0x80001000, [0x0C000800, 0, 0x0C000800, 0, 0x03E00008, 0]),
                "leaf": (0x80002000, [0x03E00008, 0]),
            }
        )
        seeds = [extract("int leaf(int value);", {"kind": "declared"})]
        expected = infer(SimpleNamespace(), machine, seeds)
        actual = infer(SimpleNamespace(), {**machine, "functions": Reads(machine["functions"])}, seeds)
        self.assertEqual(actual, expected)
        self.assertEqual(counts, {"caller": 1, "leaf": 1})

    def test_array_and_pointer_declarators_are_valid_c(self) -> None:
        self.assertEqual(declarator("unsigned char[4]", "arg0"), "unsigned char arg0[4]")
        self.assertEqual(declarator("int[3][4]", "matrix"), "int matrix[3][4]")
        extract("void leaf(" + declarator("unsigned char[4]", "arg0") + ");", {})

    def test_proven_source_does_not_claim_included_globals_or_arrays(self) -> None:
        source = '# 1 "shared/prototypes.h"\nextern float table[];\n# 1 "owned.c"\nint leaf(void) { return 1; }\n'
        seed = extract(source, {"kind": "proven"}, definitions=True, owned_source=Path("owned.c"))
        self.assertEqual(set(seed["functions"]), {"leaf"})
        self.assertEqual(seed["globals"], {})
        self.assertEqual(seed["arrays"], {})

    def test_width_placeholder_typedef_does_not_become_a_semantic_type(self) -> None:
        result = solve(
            {"leaf": (0x80001000, [0x03E00008, 0])},
            "typedef int M2C_UNK; M2C_UNK leaf(M2C_UNK p); extern M2C_UNK global;",
        )
        self.assertEqual(result["functions"]["leaf"]["state"], "unknown")
        self.assertIsNone(result["functions"]["leaf"]["prototype"])
        self.assertEqual(result["globals"]["global"]["state"], "unknown")

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
        self.assertIn("int field_0;", shape["declaration"])
        self.assertTrue(shape["partial"])

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
        self.assertEqual(caller["arity"], 1)
        self.assertEqual(result["nodes"]["field:param:caller:r4:4"]["type"], "float")
        self.assertEqual(result["structs"]["Shared"]["users"], ["caller", "leaf"])
        self.assertTrue(any(row["key"] == "types.conflict:abi:caller" for row in result["conflicts"]))

    def test_word_access_to_an_opaque_array_does_not_seed_a_byte_scalar(self) -> None:
        result = solve(
            {
                "caller": (0x80001000, [0x00808021, 0x0C000800, 0x02002021, 0x8E020004, 0x03E00008, 0]),
                "leaf": (0x80002000, [0x8C820000, 0x03E00008, 0]),
            },
            "struct Shared { unsigned char unknown_0[4]; int count; }; int leaf(struct Shared *p);",
        )
        self.assertEqual(result["nodes"]["field:param:caller:r4:0"]["type"], "int")
        self.assertEqual(result["structs"]["Shared"]["fields"][0]["extent"], [4])

    def test_equal_offsets_and_parameter_names_do_not_create_a_shared_type(self) -> None:
        result = solve(
            {"first": (0x80001000, [0x8C820004, 0x03E00008, 0]), "second": (0x80002000, [0x8C820004, 0x03E00008, 0])},
            "",
        )
        self.assertFalse(result["structs"])
        self.assertEqual(result["functions"]["first"]["return"]["type"], "int")
        self.assertEqual(result["functions"]["first"]["prototype"], "int first(void * arg0);")

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

        self.assertEqual(registers(["float", "double", "int"]), ["f12", "f14", "stack16"])
        self.assertEqual(registers(["int", "double"]), ["r4", "r6"])
        self.assertEqual(registers(["double", "int"]), ["f12", "r6"])
        self.assertEqual(registers(["int", "float"]), ["r4", "r5"])

    def test_version_signature_conflict_remains_visible(self) -> None:
        seeds = [extract("int leaf(int *p);", {"version": "us"}), extract("int leaf(float *p);", {"version": "eu"})]
        result = infer(SimpleNamespace(), facts({"leaf": (0x80001000, [0x03E00008, 0])}), seeds)
        self.assertEqual(result["functions"]["leaf"]["state"], "conflict")
        self.assertGreaterEqual(len(result["conflicts"]), 2)

    def test_identical_cross_version_declarations_do_not_conflict(self) -> None:
        source = "struct Shared { int count; }; int leaf(int *p); extern int data; extern float table[12];"
        seeds = [extract(source, {"version": "us"}), extract(source, {"version": "eu"})]
        result = infer(SimpleNamespace(), facts({"leaf": (0x80001000, [0x03E00008, 0])}), seeds)
        self.assertFalse(result["conflicts"])
        self.assertEqual(result["functions"]["leaf"]["state"], "known")
        self.assertEqual(result["structs"]["Shared"]["state"], "known")
        self.assertEqual(result["arrays"]["table"]["state"], "known")


class MachineEvidenceTests(unittest.TestCase):
    def test_loads_from_one_shared_pointer_field_preserve_common_provenance(self) -> None:
        mapped = facts(
            {
                "first": (0x80001000, [0x8C880004, 0x81020009, 0x03E00008, 0]),
                "second": (0x80002000, [0x8C880004, 0x9102000A, 0x03E00008, 0]),
            }
        )
        mapped["globals"] = {"source": {"versions": {"us": {"address": 0x80003000}}, "accesses": []}}
        for body in mapped["functions"].values():
            body["versions"]["us"]["memory"][0]["base"]["origins"] = [{"id": "global:source", "offset": 0}]
        result = infer(SimpleNamespace(), mapped, [])
        shapes = {shape["common_base"]: shape for shape in result["structs"].values()}
        self.assertEqual(set(shapes), {"global:source", "field:global:source:4"})
        nested = shapes["field:global:source:4"]
        self.assertEqual(nested["state"], "known")
        self.assertEqual(nested["users"], ["first", "second"])
        self.assertIn("signed char field_9;", nested["declaration"])
        self.assertIn("unsigned char field_A;", nested["declaration"])
        self.assertEqual(nested["base_nodes"], ["field:global:source:4", "memory:first:us:0", "memory:second:us:0"])

    def test_observed_global_layout_does_not_require_a_semantic_pointer_type(self) -> None:
        mapped = facts(
            {
                "first": (0x80001000, [0x8C820004, 0x03E00008, 0]),
                "second": (0x80002000, [0x9482000A, 0x03E00008, 0]),
            }
        )
        mapped["globals"] = {"source": {"versions": {"us": {"address": 0x80003000}}, "accesses": []}}
        for body in mapped["functions"].values():
            body["versions"]["us"]["memory"][0]["base"]["origins"] = [{"id": "global:source", "offset": 0}]
        result = infer(SimpleNamespace(), mapped, [extract("extern int source;", {"kind": "proven"})])
        shape = next(iter(result["structs"].values()))
        self.assertEqual(shape["state"], "known")
        self.assertEqual(shape["common_base"], "global:source")
        self.assertEqual(shape["minimum_size"], 12)
        self.assertIsNone(shape["size"])
        self.assertIn("int field_4;", shape["declaration"])
        self.assertIn("unsigned char padding_8[2];", shape["declaration"])
        self.assertIn("unsigned short field_A;", shape["declaration"])
        self.assertEqual(result["globals"]["source"]["type"], "int")
        parsed = extract(shape["declaration"], {})["structs"]
        fields = next(iter(parsed.values()))["fields"]
        self.assertEqual([(field["offset"], field["size"]) for field in fields], [(0, 4), (4, 4), (8, 2), (10, 2)])
        name = next(iter(result["structs"]))
        proven = extract(
            f"struct {name} {{ unsigned char padding_0[4]; float value; unsigned short count; }};",
            {"kind": "proven"},
        )
        repeated = infer(SimpleNamespace(), mapped, [proven])
        retained = repeated["structs"][name]
        self.assertEqual(retained["state"], "known")
        self.assertEqual(retained["common_base"], "global:source")
        self.assertIn("float value;", retained["declaration"])
        self.assertEqual(retained["fields"][0]["name"], "value")
        self.assertEqual(retained["fields"][0]["type"], "float")
        self.assertIsNone(retained["size"])

    def test_layout_widths_keep_conflicting_fields_opaque_and_bad_intervals_named(self) -> None:
        from unbake.typemap.layouts import observed

        def access(opcode, width=4, sign=True, partial=False):
            return {"function": "first", "opcode": opcode, "width": width, "signedness": sign, "partial": partial}

        shape = observed("Shape_test", "global:source", {4: [access(0x23), access(0x31)]}, {}, ["first", "second"])
        self.assertEqual(shape["state"], "known")
        self.assertIn("unsigned char unknown_4[4];", shape["declaration"])
        self.assertEqual(shape["fields"][0]["state"], "unknown")
        self.assertIn("conflicting", shape["fields"][0]["reason"])
        for offsets, reason in (
            ({-4: [access(0x23)]}, "negative"),
            ({0: [access(0x23)], 2: [access(0x25, 2, False)]}, "overlapping"),
            ({0: [access(0x23), access(0x25, 2, False)]}, "inconsistent"),
            ({0: [access(0x22, partial=True)]}, "partial memory"),
        ):
            with self.subTest(reason=reason):
                shape = observed("Shape_test", "global:source", offsets, {}, ["first", "second"])
                self.assertEqual(shape["state"], "unknown")
                self.assertIsNone(shape["declaration"])
                self.assertIn(reason, shape["reason"])

    def test_seedless_integer_signature(self) -> None:
        result = solve({"add": (0x80001000, [0x00851020, 0x03E00008, 0])}, "")
        self.assertEqual(result["functions"]["add"]["prototype"], "int add(int arg0, int arg1);")

    def test_seedless_float_signature_and_float_return(self) -> None:
        result = solve({"add": (0x80001000, [0x460E6000, 0x03E00008, 0])}, "")
        self.assertEqual(result["functions"]["add"]["prototype"], "float add(float arg0, float arg1);")

    def test_callsite_unknown_blocks_known_signature(self) -> None:
        result = solve(
            {
                "caller": (0x80001000, [0x0C000C00, 0, 0x0C000800, 0, 0x03E00008, 0]),
                "leaf": (0x80002000, [0x00851020, 0x03E00008, 0]),
            },
            "",
        )
        self.assertIsNone(result["functions"]["leaf"]["prototype"])
        self.assertTrue(result["functions"]["leaf"]["abi"]["missing"])

    def test_fp_word_result_does_not_claim_an_integer_return_abi(self) -> None:
        # trunc.w.s f0,f12 leaves an integer representation in an FP register.
        result = solve({"word": (0x80001000, [0x4600600D, 0x03E00008, 0])}, "")
        self.assertIsNone(result["functions"]["word"]["prototype"])
        self.assertTrue(any("return ABI" in row.get("reason", "") for row in result["conflicts"]))

    def test_float_bits_in_leading_gpr_do_not_claim_a_hard_float_signature(self) -> None:
        # mtc1 a0,f0; add.s f0,f0,f0; jr ra; nop
        result = solve({"bits": (0x80001000, [0x44840000, 0x46000000, 0x03E00008, 0])}, "")
        self.assertIsNone(result["functions"]["bits"]["prototype"])
        self.assertTrue(any("parameter ABI" in row.get("reason", "") for row in result["conflicts"]))

    def test_stack_arguments_have_entry_sp_provenance(self) -> None:
        # lw v0,16(sp); jr ra; nop
        result = solve({"fifth": (0x80001000, [0x8FA20010, 0x03E00008, 0])}, "")
        self.assertEqual(result["functions"]["fifth"]["return"]["type"], "int")
        self.assertIn("stack16", result["functions"]["fifth"]["abi"]["registers"])
        self.assertIsNone(result["functions"]["fifth"]["prototype"])

    def test_unsigned_and_float_globals_derive_from_all_accessors(self) -> None:
        mapped = facts({"reader": (0x80001000, [0x90820000, 0xC4800004, 0x03E00008, 0])})
        memory = mapped["functions"]["reader"]["versions"]["us"]["memory"]
        mapped["globals"] = {
            name: {"versions": {"us": {"address": 0x80003000 + i * 4}}, "accesses": []}
            for i, name in enumerate(("byte", "real"))
        }
        for access, name in zip(memory, ("byte", "real"), strict=True):
            access["symbols"] = [name]
        result = infer(SimpleNamespace(), mapped, [])
        self.assertEqual(result["globals"]["byte"]["type"], "unsigned char")
        self.assertEqual(result["globals"]["real"]["type"], "float")
        self.assertEqual(result["globals"]["real"]["declaration"], "extern float real;")

    def test_conflicting_global_accesses_are_named(self) -> None:
        mapped = facts({"reader": (0x80001000, [0x80820000, 0x90830000, 0x03E00008, 0])})
        mapped["globals"] = {"data": {"versions": {"us": {"address": 0x80003000}}, "accesses": []}}
        for access in mapped["functions"]["reader"]["versions"]["us"]["memory"]:
            access["symbols"] = ["data"]
        result = infer(SimpleNamespace(), mapped, [])
        self.assertEqual(result["globals"]["data"]["state"], "conflict")
        self.assertTrue(any("global:data" in row["key"] for row in result["conflicts"]))

    def test_proven_c_overrides_incompatible_machine_and_declaration(self) -> None:
        mapped = facts({"leaf": (0x80001000, [0x8C820000, 0x03E00008, 0])})
        result = infer(
            SimpleNamespace(),
            mapped,
            [
                extract("int leaf(int *p);", {"kind": "declared"}),
                extract("unsigned int leaf(float *p);", {"kind": "proven"}),
            ],
        )
        self.assertEqual(result["functions"]["leaf"]["prototype"], "unsigned int leaf(float *p);")
        self.assertEqual(result["functions"]["leaf"]["params"][0]["type"], "float *")
        self.assertFalse(result["conflicts"])

    def test_array_stride_and_element_known_with_unknown_extent(self) -> None:
        mapped = facts({"reader": (0x80001000, [0xC4800000, 0x03E00008, 0])})
        access = mapped["functions"]["reader"]["versions"]["us"]["memory"][0]
        access["indexed"] = {"anchor": 0x80003000, "scale": 4, "extent": None}
        mapped["globals"] = {"table": {"versions": {"us": {"address": 0x80003000}}, "accesses": []}}
        result = infer(SimpleNamespace(), mapped, [])
        self.assertEqual(result["arrays"]["table"]["state"], "known")
        self.assertEqual(result["arrays"]["table"]["type"], "float")
        self.assertIsNone(result["arrays"]["table"]["extent"])

    def test_indexed_pointer_element_uses_the_value_propagation_graph(self) -> None:
        # lw v0,0(a0); lw v0,0(v0); jr ra; nop
        mapped = facts({"reader": (0x80001000, [0x8C820000, 0x8C420000, 0x03E00008, 0])})
        accesses = mapped["functions"]["reader"]["versions"]["us"]["memory"]
        accesses[0]["indexed"] = {"anchor": 0x80003000, "scale": 4, "extent": None}
        accesses[0]["symbols"] = ["table"]
        mapped["globals"] = {"table": {"versions": {"us": {"address": 0x80003000}}, "accesses": []}}
        result = infer(SimpleNamespace(), mapped, [])
        self.assertEqual(result["arrays"]["table"]["type"], "void *")
        self.assertEqual(result["arrays"]["table"]["state"], "known")
        self.assertIsNone(result["arrays"]["table"]["extent"])

    def test_shared_layout_padding_and_explicit_unknown_field(self) -> None:
        result = solve(
            {
                "caller": (0x80001000, [0x00808021, 0x0C000800, 0x02002021, 0xAE000008, 0x03E00008, 0]),
                "leaf": (0x80002000, [0x8C820000, 0x03E00008, 0]),
            },
            "",
        )
        shape = next(iter(result["structs"].values()))
        self.assertTrue(shape["partial"])
        self.assertIn("padding_4[4]", shape["declaration"])
        self.assertIn("int field_0", shape["declaration"])
        self.assertIsNone(shape["size"])

    def test_cross_version_input_conflict_is_named(self) -> None:
        mapped = facts({"leaf": (0x80001000, [0x00851020, 0x03E00008, 0])})
        mapped["functions"]["leaf"]["versions"]["eu"] = {
            "address": 0x80001000,
            **Analysis("leaf", "eu", 0x80001000, 0x40, [0x00861020, 0x03E00008, 0], {}, {}).run(),
        }
        result = infer(SimpleNamespace(), mapped, [])
        self.assertEqual(result["functions"]["leaf"]["state"], "conflict")
        self.assertTrue(any(row["key"] == "types.conflict:abi:leaf" for row in result["conflicts"]))


if __name__ == "__main__":
    unittest.main()
