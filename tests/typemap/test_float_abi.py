"""Real O32 floating word consumers and their fuzzy entry admission."""

import json
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from unbake import land
from unbake.typemap.mips import Analysis
from unbake.typemap.solver import infer

FIXTURE = Path(__file__).parents[1] / "fixtures/battletanx_float_abi"


def real_facts():
    data = json.loads((FIXTURE / "machine.json").read_text())
    targets = {p["address"]: n for n, p in data["programs"].items()}
    functions = {}
    for name, p in data["programs"].items():
        body = Analysis(name, "us", p["address"], p["rom_offset"], [int(w, 16) for w in p["words"]], targets, {}).run()
        functions[name] = {"versions": {"us": {"address": p["address"], **body}}}
    caller = data["caller"]
    functions[caller["name"]] = {"versions": {"us": caller["body"]}}
    return {"functions": functions, "globals": {}}


class FloatAbiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.result = infer(SimpleNamespace(), real_facts(), [])

    def test_real_callee_slot_signatures(self):
        for name, pointers in (("func_8010AA6C_us", 2), ("func_8010BC10_us", 1)):
            with self.subTest(function=name):
                record = self.result["functions"][name]
                self.assertEqual([p["type"] for p in record["params"][:8]], ["float"] * 8)
                self.assertTrue(all(p["state"] == "known" for p in record["params"]))
                self.assertEqual(len(record["params"]), 8 + pointers)
                self.assertEqual(record["return"]["type"], "int")
                self.assertTrue(record["abi"]["machine_return_known"])

    def test_real_drafts_pass_fuzzy_entry_admission(self):
        for name in ("func_8010AA6C_us", "func_8010BC10_us"):
            with (
                self.subTest(function=name),
                patch(
                    "unbake.decomp.draft_abi.mapped_body",
                    return_value=real_facts()["functions"][name]["versions"]["us"],
                ),
                patch("unbake.typemap.types_db.path", return_value=Path("unused.sqlite")),
                patch("unbake.typemap.types_db.entries", return_value={name: self.result["functions"][name]}),
                patch("unbake.typemap.types_db.meta", return_value={}),
            ):
                project = SimpleNamespace(compiler_for=lambda function: SimpleNamespace(id="gcc-2.7.2-kmc"))
                land._fuzzy_signature(project, name, "us", (FIXTURE / (name + ".c")).read_text())

    def test_slot_signature_is_known_without_guessing_aggregate_boundaries(self):
        for name in ("func_8010AA6C_us", "func_8010BC10_us"):
            record = self.result["functions"][name]
            self.assertEqual(record["machine_signature"]["state"], "known")
            self.assertEqual(record["machine_signature"]["return"], {"register": "r2", "type": "int"})
            self.assertIsNone(record["prototype"])
            self.assertEqual(record["state"], "conflict")
            self.assertIn("parameter ABI disagrees with observed scalar representations", record["abi"]["conflicts"])
            self.assertNotIn("types.abi.bits", str(record.get("abi_declaration")))

    def test_fuzzy_rejects_incorrect_word_types_and_scalar_fp_convention(self):
        from unbake.config import Held

        for name in ("func_8010AA6C_us", "func_8010BC10_us"):
            source = (FIXTURE / (name + ".c")).read_text()
            record = self.result["functions"][name]
            with (
                patch("unbake.decomp.draft_abi.mapped_body", return_value=object()),
                patch("unbake.typemap.types_db.path", return_value=Path("unused.sqlite")),
                patch("unbake.typemap.types_db.entries", return_value={name: record}),
                patch("unbake.typemap.types_db.meta", return_value={}),
            ):
                for bad in (
                    source.replace("f32 y; f32 x;", "int y; int x;"),
                    source.replace("f32 y; f32 x;", "f32 y; int padding; f32 x;"),
                    source.replace("s32 " + name, "f32 " + name),
                    "int "
                    + name
                    + "(float a, float b, float c, float d, float e, float f, float g, float h, void *out)"
                    + " { return 0; }",
                ):
                    with self.subTest(function=name, source=bad[:60]), self.assertRaisesRegex(Held, "land.fuzzy_abi"):
                        land._fuzzy_signature(SimpleNamespace(), name, "us", bad)

    def test_missing_conflicting_or_ambiguous_slots_do_not_admit(self):
        import copy

        from unbake.config import Held

        name = "func_8010BC10_us"
        for field in ("params", "return", "missing"):
            record = copy.deepcopy(self.result["functions"][name])
            record["machine_signature"]["state"] = "unknown"
            if field == "params":
                record["params"][0].update(state="conflict", type=None)
            elif field == "return":
                record["return"].update(state="unknown", type=None)
            else:
                record["abi"]["missing"] = [{"register": "r4"}]
            with (
                self.subTest(field=field),
                patch("unbake.decomp.draft_abi.mapped_body", return_value=object()),
                patch("unbake.typemap.types_db.path", return_value=Path("unused.sqlite")),
                patch("unbake.typemap.types_db.entries", return_value={name: record}),
                patch("unbake.typemap.types_db.meta", return_value={}),
                self.assertRaisesRegex(Held, "land.fuzzy_abi"),
            ):
                land._fuzzy_signature(SimpleNamespace(), name, "us", (FIXTURE / (name + ".c")).read_text())


class GenericFloatTests(unittest.TestCase):
    def test_fp_argument_and_return_widths_are_resolved_from_callee_ops(self):
        from tests.typemap.test_solver import solve

        for opcode, type_ in ((0x460E6000, "float"), (0x462E6000, "double")):
            with self.subTest(type=type_):
                record = solve({"add": (0x80001000, [opcode, 0x03E00008, 0])}, "")["functions"]["add"]
                self.assertEqual([p["type"] for p in record["params"]], [type_, type_])
                self.assertEqual(record["return"]["type"], type_)
                self.assertEqual(record["state"], "known")

    def test_mtc1_mfc1_preserve_float_semantics_and_gpr_abi_conflict(self):
        from tests.typemap.test_solver import solve

        # mtc1 a1,f4; add.s f0,f12,f4; mfc1 v0,f0; jr ra
        record = solve(
            {
                "leaf": (0x80001000, [0x44852000, 0x46046000, 0x44020000, 0x03E00008, 0]),
                "caller": (0x80002000, [0x0C000400, 0, 0xAC820000, 0x03E00008, 0]),
            },
            "",
        )["functions"]["leaf"]
        self.assertEqual([p["type"] for p in record["params"]], ["float", "float"])
        self.assertEqual(record["return"]["type"], "float")
        self.assertIsNone(record["prototype"])
        self.assertTrue(record["abi"]["conflicts"])

    def test_stack_fp_reload_resolves_the_spilled_register_not_a_new_argument(self):
        from tests.typemap.test_solver import solve

        # sw a0,16(sp); lwc1 f0,16(sp); jr ra
        record = solve({"leaf": (0x80001000, [0xAFA40010, 0xC7A00010, 0x03E00008, 0])}, "")["functions"]["leaf"]
        self.assertEqual([p["register"] for p in record["params"]], ["r4"])
        self.assertEqual(record["params"][0]["type"], "float")
        self.assertIsNone(record["prototype"])

    def test_swc1_consumes_an_fp_input_without_float_arithmetic(self):
        from tests.typemap.test_solver import solve

        record = solve({"leaf": (0x80001000, [0xE7ACFFF0, 0x03E00008, 0])}, "")["functions"]["leaf"]
        self.assertEqual(record["prototype"], "void leaf(float arg0);")

    def test_integer_operations_still_conflict_with_float_consumption(self):
        from tests.typemap.test_solver import solve

        # andi t0,a0,1; mtc1 a0,f0; add.s f0,f0,f0; jr ra
        record = solve({"leaf": (0x80001000, [0x30880001, 0x44840000, 0x46000000, 0x03E00008, 0])}, "")["functions"][
            "leaf"
        ]
        self.assertEqual(record["params"][0]["state"], "conflict")
        self.assertEqual(record["params"][0]["alternatives"], ["float", "int"])
        self.assertEqual(record["machine_signature"]["state"], "unknown")

    def test_refined_map_overlays_value_and_stack_provenance(self):
        import copy

        from unbake.typemap.abi_facts import Functions

        mapped = real_facts()["functions"]
        name = "func_8010AA6C_us"
        fresh = mapped[name]["versions"]["us"]
        stale = copy.deepcopy(mapped[name])
        stale["versions"]["us"]["value_types"] = []
        for memory in stale["versions"]["us"]["memory"]:
            memory.pop("loaded", None)
        record = {k: fresh[k] for k in ("register_inputs", "register_outputs", "value_types")}
        record.update(
            memory={str(m["instruction"]): m for m in fresh["memory"]},
            calls={},
            returns={str(r["instruction"]): r["values"] for r in fresh["returns"]},
        )
        overlaid = Functions({name: stale}, {name: {"versions": {"us": record}}})[name]
        self.assertEqual(overlaid["versions"]["us"], fresh)

    def test_joined_fp_load_returns_keep_width_and_conflicts(self):
        from tests.typemap.test_solver import solve

        # bnez a0,path2; nop; load f0,0(a1); b exit; nop;
        # path2: load f0,0(a2); exit: jr ra; nop
        for first, second, type_ in ((0xC4A00000, 0xC4C00000, "float"), (0xD4A00000, 0xD4C00000, "double")):
            with self.subTest(type=type_):
                programs = {"leaf": (0x80001000, [0x14800004, 0, first, 0x10000002, 0, second, 0x03E00008, 0])}
                record = solve(programs, "")["functions"]["leaf"]
                self.assertEqual(record["return"]["type"], type_)
        mixed = solve({"leaf": (0x80001000, [0x14800004, 0, 0xC4A00000, 0x10000002, 0, 0xD4C00000, 0x03E00008, 0])}, "")
        self.assertEqual(mixed["functions"]["leaf"]["return"]["state"], "conflict")
        self.assertEqual(mixed["functions"]["leaf"]["machine_signature"]["state"], "unknown")

    def test_conflicting_semantics_cannot_be_admitted_as_an_integer_carrier(self):
        from tests.typemap.test_solver import solve
        from unbake.config import Held

        programs = {"leaf": (0x80001000, [0x30880001, 0x44840000, 0x46000080, 0x24020001, 0x03E00008, 0])}
        record = solve(programs, "")["functions"]["leaf"]
        self.assertEqual(record["state"], "conflict")
        with (
            patch("unbake.decomp.draft_abi.mapped_body", return_value=object()),
            patch("unbake.typemap.types_db.path", return_value=Path("unused.sqlite")),
            patch("unbake.typemap.types_db.entries", return_value={"leaf": record}),
            patch("unbake.typemap.types_db.meta", return_value={}),
            self.assertRaisesRegex(Held, "land.fuzzy_abi"),
        ):
            land._fuzzy_signature(SimpleNamespace(), "leaf", "us", "int leaf(int value) { return value; }")
