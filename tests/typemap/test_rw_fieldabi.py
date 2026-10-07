"""Measured Rage Wars layouts and declared by-value transport, all held versions."""

import copy
import hashlib
import json
import struct
import unittest
from collections import UserDict
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from unbake import cdecl
from unbake.typemap import abi_declarations, declarations, layouts, o32
from unbake.typemap.mips import Analysis
from unbake.typemap.solver import infer

FIXTURE = Path(__file__).parents[1] / "fixtures/ragewars_fieldabi"
VERSIONS = ["de", "eu", "eu-x", "us", "us-rev1"]
WORDS = ["r4", "r5", "r6", "r7", "stack16", "stack20", "stack24", "stack28", "stack32", "stack36", "stack40"]


def payload():
    fixture = json.loads((FIXTURE / "machine.json").read_text())
    functions = {}
    byte_count = 0
    for name, versions in fixture["programs"].items():
        functions[name] = {"versions": {}}
        for version, row in versions.items():
            data = bytes.fromhex(row["hex"])
            byte_count += len(data)
            assert hashlib.sha256(data).hexdigest() == row["target_sha256"]
            words = list(struct.unpack(">" + str(len(data) // 4) + "I", data))
            targets = {int(address): target for address, target in row["targets"].items()}
            body = Analysis(name, version, row["address"], row["rom_offset"], words, targets, {}).run()
            functions[name]["versions"][version] = {"address": row["address"], **body}
    assert byte_count == fixture["bytes"] == 38528
    return {"functions": functions, "globals": {}}


def aggregate_seed():
    return declarations.extract(
        (FIXTURE / "aggregate.h").read_text() + (FIXTURE / "func_80267F98_de.c").read_text(),
        {"kind": "published"},
    )


class RwFieldAbiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        run = Analysis.run
        with patch.object(Analysis, "run", autospec=True, side_effect=run) as analyze:
            cls.facts = payload()
        cls.analyzer_calls = analyze.call_count

    def test_real_falloff_word_survives_the_branch_zero_join(self):
        for version, body in self.facts["functions"]["func_80266810_de"]["versions"].items():
            with self.subTest(version=version):
                self.assertIn("stack32", body["register_inputs"])
                loads = [
                    row
                    for row in body["memory"]
                    if row.get("loaded", {}).get("origins") == [{"id": "param:func_80266810_de:stack32", "offset": 0}]
                ]
                self.assertEqual(len(loads), 1)
                self.assertEqual(loads[0]["width"], 4)

    def test_fixture_work_is_sixty_five_bounded_real_bodies(self):
        self.assertEqual(self.analyzer_calls, 65)
        self.assertEqual(sum(len(item["versions"]) for item in self.facts["functions"].values()), 65)
        for item in self.facts["functions"].values():
            self.assertEqual(list(item["versions"]), VERSIONS)

    def test_real_grouped_contract_seeds_eleven_words_without_shifted_formals(self):
        names = {"func_80266810_de", "func_80267F98_de"}
        bodies = {n: row for n, row in self.facts["functions"].items() if n in names}
        counts = {}

        class Reads(UserDict):
            def __init__(self, records):
                super().__init__(records)
                # Real map shards keep code interval metadata independently
                # of decoded bodies, including the namespace identity pass.
                self.inventory = {
                    name: {
                        "versions": {version: {"address": body["address"]} for version, body in row["versions"].items()}
                    }
                    for name, row in records.items()
                }

            def __getitem__(self, key):
                counts[key] = counts.get(key, 0) + 1
                return super().__getitem__(key)

        result = infer(SimpleNamespace(), {"functions": Reads(bodies), "globals": {}}, [aggregate_seed()])
        record = result["functions"]["func_80266810_de"]
        self.assertEqual([p["register"] for p in record["params"]], WORDS)
        self.assertEqual(record["arity"], 8)
        self.assertTrue(record["transport_known"])
        self.assertEqual(len(record["source_params"]), 8)
        for word in WORDS[3:]:
            self.assertIn("param:func_80266810_de:" + word, result["nodes"])
        self.assertEqual(counts, {n: 3 for n in names})
        self.assertEqual(record["versions"], VERSIONS)
        # r6 is a declared but unconsumed word. No missing value or caller
        # expression is invented to populate it.
        self.assertNotIn("r6", record["abi"]["registers"])
        self.assertFalse(record["abi"]["missing"])
        self.assertIn("Triple", record.get("prototype") or record["abi_declaration"]["prototype"])
        reconciliation = record["entry_reconciliation"]
        self.assertEqual([word["register"] for word in reconciliation["unread_declared_words"]], ["r6"])
        self.assertFalse(reconciliation["measured_declaration"]["parameters_known"])
        self.assertIn("Triple", reconciliation["canonical_prototype"])

    def test_struct_members_use_gprs_and_do_not_invent_padding_or_union_transport(self):
        seed = aggregate_seed()
        signature = seed["functions"]["func_80266810_de"]
        slots = o32.declared_slots(signature, seed["structs"], seed["aliases"])
        self.assertEqual([p["register"] for p in slots], WORDS)
        self.assertEqual([p["parameter"] for p in slots], [0, 1, 2, 3, 3, 3, 4, 4, 5, 6, 7])
        self.assertEqual([p["offset"] for p in slots[3:6]], [0, 4, 8])
        for change in ("missing", "padding", "conflict", "union"):
            changed = copy.deepcopy(seed)
            if change == "missing":
                del changed["structs"]["Triple"]
            elif change == "padding":
                changed["structs"]["Triple"]["fields"][1]["offset"] = 8
            elif change == "conflict":
                changed["structs"]["Triple"]["declaration_conflict"] = True
            else:
                changed["aliases"]["Triple"] = "union Triple"
            with self.subTest(change=change):
                self.assertIsNone(o32.declared_slots(signature, changed["structs"], changed["aliases"]))
        floating = declarations.extract("struct Vec { float x; float y; float z; }; void entry(struct Vec p);", {})
        self.assertEqual(
            [p["register"] for p in o32.declared_slots(floating["functions"]["entry"], floating["structs"], {})],
            ["r4", "r5", "r6"],
        )

    def test_supplemental_fields_keep_authored_identity_and_unknown_extent(self):
        names = {"func_8023A4E0_de", "func_8023B3F8_de", "func_80296930_de"}
        source = (
            (FIXTURE / "fields.h").read_text()
            + """
void func_8023A4E0_de(struct Object_func_8023B3F8_de *, struct View_func_8023B3F8_de *);
void func_8023B3F8_de(struct Scene *, struct View_func_8023B3F8_de *);
f32 func_80296930_de(f32 *, f32 *, f32 *);
"""
        )
        seed = declarations.extract(source, {"kind": "published"})
        facts = {"functions": {n: row for n, row in self.facts["functions"].items() if n in names}, "globals": {}}
        result = infer(SimpleNamespace(), facts, [seed])
        for name in ("Object_func_8023B3F8_de", "View_func_8023B3F8_de"):
            self.assertEqual(result["structs"][name]["declaration"], seed["structs"][name]["declaration"])
        storage = [row for row in result["structs"].values() if row.get("canonical_pointer")]
        objects = [row for row in storage if row["canonical_pointer"] == "struct Object_func_8023B3F8_de *"]
        self.assertTrue(objects, "the declared entry identity supports measured fields of its one real reader")
        object_ = objects[0]
        self.assertTrue(object_["partial"])
        self.assertIsNone(object_["size"])
        self.assertEqual(object_["source_binding"], "callee entry only; caller object identity unresolved")
        self.assertNotIn("param:func_8023B3F8_de:r4", object_["base_nodes"])
        self.assertTrue({0xC, 0x1C, 0x20, 0x208, 0x20C, 0x210, 0x218, 0x220} <= set(object_["observed_offsets"]))
        fields = {f["offset"]: f for f in object_["fields"]}
        self.assertEqual(fields[0x208]["type"], "float")
        self.assertEqual(fields[0x220]["type"], "float")
        views = [row for row in storage if row["canonical_pointer"] == "struct View_func_8023B3F8_de *"]
        self.assertTrue(views, "authored hidden/depth fields must not suppress real corner/plane observations")
        view = views[0]
        self.assertTrue(view["partial"])
        self.assertIsNone(view["size"])
        self.assertIn("func_8023A4E0_de", view["users"])
        self.assertIn(0x264, view["observed_offsets"])
        self.assertTrue({0x310, 0x314, 0x318, 0x31C} <= set(view["observed_offsets"]))
        self.assertNotIn("param:func_80296930_de:r4", view["base_nodes"])
        measured = {f["offset"]: f for f in view["fields"]}
        self.assertEqual(measured[0x264]["type"], "float")
        self.assertEqual(measured[0x310]["type"], "float")
        # Parsing the whole supplemental declaration checks its target field
        # offsets; unknown array count and Vec3 identity are not guessed.
        native = cdecl.records(view["declaration"])[0]
        self.assertEqual({f.offset: f.size for f in native.fields if not f.name.startswith("padding")}[0x310], 4)

    def test_conflicting_interior_offsets_cannot_select_one_common_storage_view(self):
        names = {"func_8023A4E0_de", "func_8023B3F8_de", "func_80296930_de"}
        facts = {
            "functions": {n: copy.deepcopy(row) for n, row in self.facts["functions"].items() if n in names},
            "globals": {},
        }
        calls = facts["functions"]["func_8023A4E0_de"]["versions"]["eu"]["calls"]
        plane = next(c for c in calls if c["callee"] == "func_80296930_de")
        plane["arguments"]["r4"]["origins"][0]["offset"] += 4
        source = (
            (FIXTURE / "fields.h").read_text()
            + """
void func_8023A4E0_de(struct Object_func_8023B3F8_de *, struct View_func_8023B3F8_de *);
void func_8023B3F8_de(struct Scene *, struct View_func_8023B3F8_de *);
f32 func_80296930_de(f32 *, f32 *, f32 *);
"""
        )
        result = infer(SimpleNamespace(), facts, [declarations.extract(source, {"kind": "published"})])
        storage = [
            row
            for row in result["structs"].values()
            if row.get("canonical_pointer") == "struct View_func_8023B3F8_de *"
        ]
        self.assertTrue(storage)
        self.assertTrue(all(0x310 not in row["observed_offsets"] for row in storage))
        self.assertTrue(all(0x314 not in row["observed_offsets"] for row in storage))

    def test_float_gpr_carrier_preserves_word_semantics_without_a_scalar_fp_prototype(self):
        record = {
            "abi": {
                "registers": ["r4", "r5"],
                "missing": [],
                "conflicts": [],
                "void": True,
                "return_known": True,
                "return_register": None,
                "used_returns": [],
                "call_sites": 1,
            },
            "params": [{"register": reg, "state": "known", "type": "float"} for reg in ("r4", "r5")],
            "return": {"state": "known", "type": "void"},
        }
        carrier = abi_declarations.prototype("entry", record, {})
        self.assertEqual(carrier["prototype"], "void entry(int, int);")
        self.assertTrue(carrier["parameters_known"])
        self.assertEqual(sum("types.abi.float_word" in r for r in carrier["reasons"]), 2)
        self.assertEqual([p["type"] for p in record["params"]], ["float", "float"])

    def test_actual_restore_caller_and_published_unused_formal_remain_distinct_evidence(self):
        facts = {
            "functions": {
                n: row
                for n, row in self.facts["functions"].items()
                if n in {"func_80200500_de", "func_80200538_de", "func_80201A74_de"}
            },
            "globals": {},
        }
        source = "typedef int s32;\n" + (FIXTURE / "func_80200500_de.c").read_text()
        record = infer(SimpleNamespace(), facts, [declarations.extract(source, {"kind": "proven"})])["functions"][
            "func_80200538_de"
        ]
        self.assertEqual(record["abi"]["registers"], ["r4"])
        self.assertFalse(record["abi"]["missing"])
        self.assertEqual(record["versions"], VERSIONS)
        self.assertEqual(len(record["params"]), 2)
        self.assertIn("arg1", record.get("prototype") or record["abi_declaration"]["prototype"])
        reconciliation = record["entry_reconciliation"]
        self.assertEqual(reconciliation["consumed_registers"], ["r4"])
        self.assertEqual(reconciliation["unread_declared_words"], [{"register": "r5", "name": "arg1", "type": "s32"}])
        self.assertEqual(reconciliation["call_sites"], 5)
        self.assertEqual(reconciliation["versions"], VERSIONS)
        self.assertEqual(reconciliation["measured_declaration"]["prototype"], "void func_80200538_de(int);")
        self.assertIn("arg1", reconciliation["canonical_prototype"])
        self.assertEqual(reconciliation["inputs"], {version: ["r4"] for version in VERSIONS})
        self.assertNotIn("operands", reconciliation)

    def test_entry_reconciliation_retains_real_missing_caller_evidence(self):
        names = {"func_80200500_de", "func_80200538_de", "func_80201A74_de"}
        facts = {
            "functions": {n: copy.deepcopy(row) for n, row in self.facts["functions"].items() if n in names},
            "globals": {},
        }
        calls = facts["functions"]["func_80201A74_de"]["versions"]["eu"]["calls"]
        call = next(row for row in calls if row["callee"] == "func_80200538_de")
        call["arguments"]["r4"]["defined"] = False
        source = "typedef int s32;\n" + (FIXTURE / "func_80200500_de.c").read_text()
        record = infer(SimpleNamespace(), facts, [declarations.extract(source, {"kind": "proven"})])["functions"][
            "func_80200538_de"
        ]
        reconciliation = record["entry_reconciliation"]
        self.assertEqual(len(reconciliation["missing"]), 1)
        self.assertEqual(reconciliation["missing"][0]["version"], "eu")
        self.assertEqual(reconciliation["missing"][0]["register"], "r4")
        self.assertTrue(any("types.abi.arguments" in r for r in reconciliation["measured_declaration"]["reasons"]))
        self.assertEqual(len(record["params"]), 2)
        self.assertIn("arg1", reconciliation["canonical_prototype"])

    def test_existing_complete_fields_do_not_emit_redundant_storage_views(self):
        accesses = [{"partial": False, "width": 4}]
        record = {"fields": [{"name": "depth", "offset": 0x210, "size": 4}]}
        self.assertTrue(layouts.covers(record, {0x210: accesses}))
        self.assertFalse(layouts.covers(record, {0x208: accesses}))
        record["fields"][0].update(name="gap210", extent=[4])
        self.assertFalse(layouts.covers(record, {0x210: accesses}))
