"""Measured code and existing function contracts cannot be published as scalar storage."""

import copy
import json
import unittest
from collections import Counter, UserDict
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from tests.typemap.test_canonical_entry import real_facts
from tests.typemap.test_solver import facts
from unbake import cdecl
from unbake.config import Held
from unbake.typemap import closure, declarations, evidence
from unbake.typemap.solver import infer

FIXTURE = Path(__file__).parents[1] / "fixtures/function_object_namespace"


class RealNamespaceTests(unittest.TestCase):
    def test_rw_measured_function_rejects_object_without_changing_entry_contract(self):
        name = "func_80429178_us_rev1"
        mapped = real_facts([name])
        contract = declarations.extract("void func_8041B110_de(int);", {"kind": "published"})
        object_ = declarations.extract("extern int " + name + ";", {"kind": "published"})
        result = infer(SimpleNamespace(), mapped, [contract, object_])
        self.assertNotIn(name, result["globals"])
        record = result["functions"][name]
        self.assertEqual(record["abi_declaration"]["prototype"], "int " + name + "(int);")
        self.assertIsNone(record["prototype"])
        self.assertEqual(len([c for c in result["constraints"] if c["kind"] == "symbol_category_conflict"]), 1)

    def test_bt_actual_468_byte_payload_rejects_scalar_and_keeps_unknown_abi_with_bounded_work(self):
        data = json.loads((FIXTURE / "machine.json").read_text())
        name = data["function"]
        body = data["versions"]["us"]
        self.assertEqual(body["end"] - body["start"], data["machine_bytes"])
        reads = Counter()

        class Bodies(UserDict):
            def __getitem__(self, name):
                reads[name] += 1
                return super().__getitem__(name)

        mapped = {"functions": Bodies({name: {"versions": data["versions"]}}), "globals": {}}
        seed = declarations.extract((FIXTURE / "object.h").read_text(), {"kind": "published"})
        before = copy.deepcopy(seed)
        with (
            patch.object(evidence, "abi", wraps=evidence.abi) as abi,
            patch.object(closure, "build", wraps=closure.build) as build,
        ):
            result = infer(SimpleNamespace(), mapped, [seed])
        self.assertNotIn(name, result["globals"])
        self.assertNotIn(name, result["arrays"])
        self.assertEqual(result["functions"][name]["state"], "unknown")
        self.assertIsNone(result["functions"][name]["prototype"])
        self.assertEqual(reads, {name: 4})
        self.assertEqual(abi.call_count, 1)
        self.assertEqual(build.call_count, 1)
        self.assertEqual(seed, before)
        self.assertNotIn("global:" + name, result["nodes"])
        self.assertNotIn("address:" + name, result["nodes"])
        conflict = [c for c in result["constraints"] if c["kind"] == "symbol_category_conflict"]
        self.assertEqual(len(conflict), 1)
        self.assertEqual(conflict[0]["declaration"], "extern s32 " + name + ";")


class NamespaceTests(unittest.TestCase):
    def test_function_contract_survives_higher_rank_scalar_receipt_in_either_order(self):
        function = declarations.extract("int dispatch(int value);", {"kind": "published"})
        object_ = declarations.extract("extern int dispatch;", {"kind": "proven"})
        for seeds in ([function, object_], [object_, function]):
            with self.subTest(order=[s["functions"] != {} for s in seeds]):
                result = infer(SimpleNamespace(), facts({}), seeds)
                self.assertEqual(result["functions"]["dispatch"]["prototype"], "int dispatch(int value);")
                self.assertNotIn("dispatch", result["globals"])

    def test_real_function_signature_disagreement_stays_conflicting(self):
        seeds = [
            declarations.extract(source, {"kind": "published"})
            for source in ("int dispatch(int value);", "void dispatch(float value);", "extern int dispatch;")
        ]
        result = infer(SimpleNamespace(), facts({}), seeds)
        self.assertEqual(result["functions"]["dispatch"]["state"], "conflict")
        self.assertIsNone(result["functions"]["dispatch"]["prototype"])
        self.assertNotIn("dispatch", result["globals"])
        self.assertTrue(any(c["kind"] == "declaration_conflict" for c in result["constraints"]))

    def test_code_aliases_and_address_symbols_are_identity_without_extra_shard_reads(self):
        from unbake.typemap import namespace, shards

        inventory = {
            "dispatch": {
                "aliases": ["dispatch_us"],
                "versions": {"us": {"address": 0x80001000, "name": "entry"}},
            }
        }
        functions = shards.Functions(Path("never-read.sqlite"), inventory)
        with patch.object(shards.Functions, "__getitem__", side_effect=AssertionError("body read")) as read:
            names, addresses = namespace.code_names(functions.inventory, {"us": {"2147487744": ["exported"]}})
        self.assertEqual(names, {"dispatch", "dispatch_us", "entry", "exported"})
        self.assertEqual(addresses, {"us": {0x80001000}})
        self.assertEqual(read.call_count, 0)
        mapped = facts({"dispatch": (0x80001000, [0x03E00008, 0])})
        mapped["functions"]["dispatch"]["aliases"] = ["dispatch_us"]
        mapped["symbols"] = {"us": {0x80001000: ["exported"]}}
        seed = declarations.extract("extern int dispatch_us[2]; extern int exported;", {"kind": "published"})
        result = infer(SimpleNamespace(), mapped, [seed])
        self.assertEqual(result["globals"], {})
        self.assertEqual(result["arrays"], {})
        self.assertEqual(len([c for c in result["constraints"] if c["kind"] == "symbol_category_conflict"]), 2)

    def test_function_pointer_objects_and_function_like_names_stay_objects(self):
        seed = declarations.extract("extern int (*callback)(int); extern int func_like;", {"kind": "published"})
        result = infer(SimpleNamespace(), facts({}), [seed])
        self.assertEqual(result["functions"], {})
        self.assertEqual(set(result["globals"]), {"callback", "func_like"})
        self.assertTrue(all(row["state"] == "known" for row in result["globals"].values()))

    def test_measured_code_and_data_disagreement_holds(self):
        mapped = facts({"dispatch": (0x80001000, [0x03E00008, 0])})
        mapped["globals"] = {"dispatch": {"versions": {"eu": {"address": 0x80200000}}, "accesses": []}}
        with self.assertRaisesRegex(Held, "types.namespace.*dispatch.*mapped object"):
            infer(SimpleNamespace(), mapped, [])


class PublicationNamespaceTests(unittest.TestCase):
    def test_retained_object_contract_holds_even_with_unknown_function_abi(self):
        from unbake.typemap import namespace

        value = {"functions": {"dispatch": {"state": "unknown"}}, "typedefs": {"Word": "int"}}
        for text in ("extern Word dispatch;", "extern int (*dispatch)(int);", "extern int dispatch[2];"):
            with self.subTest(text=text), self.assertRaisesRegex(Held, "headers.namespace.*dispatch.*object"):
                namespace.check(value, {Path("retained.h"): text})

    def test_function_typedef_and_tag_namespace_remain_compatible(self):
        from unbake.typemap import namespace

        namespace.check(
            {"functions": {"dispatch": {}}, "typedefs": {}},
            {
                Path("contract.h"): "typedef int Entry(int); extern Entry dispatch; "
                "struct dispatch { int field; }; extern int unrelated;"
            },
        )

    def test_typedef_uses_the_same_ordinary_namespace_as_functions(self):
        from unbake.typemap import namespace

        with self.assertRaisesRegex(Held, "headers.namespace.*dispatch.*typedef"):
            namespace.check({"functions": {"dispatch": {}}}, {Path("contract.h"): "typedef int dispatch;"})

    def test_renderer_holds_retained_scalar_before_planning_headers(self):
        from unbake.typemap import database

        path = Path("include/retained.h")
        session = SimpleNamespace(
            authored={},
            source_names=lambda consumers: set(),
            published={path: "extern int dispatch;"},
            published_homes={path: {path}},
        )
        value = {"functions": {"dispatch": {"state": "unknown"}}, "globals": {}, "structs": {}, "arrays": {}}
        with self.assertRaisesRegex(Held, "headers.namespace.*dispatch.*object"):
            database._render(SimpleNamespace(include=(Path("include"),)), value, None, session)

    def test_existing_function_cannot_be_overwritten_by_known_global_record(self):
        from unbake.typemap import namespace

        with self.assertRaisesRegex(Held, "headers.namespace.*dispatch"):
            namespace.check({"functions": {"dispatch": {}}, "globals": {"dispatch": {}}}, {})

    def test_many_retained_receipts_parse_one_relevant_declaration_and_skip_other_symbols(self):
        from unbake.typemap import namespace

        texts = {Path(f"receipt{i}.h"): "extern int dispatch(int); extern int unrelated;" for i in range(5)}
        with patch.object(cdecl, "parse", wraps=cdecl.parse) as parse:
            namespace.check({"functions": {"dispatch": {}}}, texts)
        self.assertEqual(parse.call_count, 1)
