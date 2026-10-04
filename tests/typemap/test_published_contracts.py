"""Published declaration ownership survives generated type refreshes."""

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from tests.typemap.test_solver import facts
from unbake.typemap import declaration_evidence, declarations
from unbake.typemap.solver import Constraints, _merge_records, infer


class PublishedContractsTests(unittest.TestCase):
    def test_consumed_imports_and_definition_are_separate_evidence(self):
        source = Path("owner.c")
        text = (
            "",
            '# 1 "contracts.h"\n'
            "typedef unsigned int Word;\n"
            "struct Queue { Word count; int entries[8]; };\n"
            "extern struct Queue queue;\nextern int table[];\n"
            "void setter(Word value);\n"
            '# 1 "owner.c"\nvoid owner(void) {setter(table[queue.count]);}\nvoid entry(void) {}\n',
        )
        consumed = declarations.published(text, {"kind": "published"}, source, contracts=True, compact=True)
        self.assertEqual(set(consumed["functions"]), {"setter", "owner", "entry"})
        self.assertEqual(set(consumed["globals"]), {"queue", "table"})
        self.assertIn("Queue", consumed["structs"])
        owned = declarations.published(text, {"kind": "proven"}, source)
        self.assertEqual(set(owned["functions"]), {"owner", "entry"})
        self.assertEqual(owned["globals"], {})

    def test_unconsumed_included_guesses_are_not_promoted_to_published_evidence(self):
        seed = declarations.extract(
            "typedef int Word; struct Used { Word word; }; struct Unused {float word;};"
            "extern struct Used table[]; extern float unrelated[];"
            "void caller(void); int callee(Word value); int guessed(float value);",
            {"kind": "published"},
        )
        retained = declarations.consumed_contracts(seed, "void caller(void) {callee(table[0].word);}")
        self.assertEqual(set(retained["functions"]), {"caller", "callee"})
        self.assertEqual(set(retained["globals"]), {"table"})
        self.assertEqual(set(retained["structs"]), {"Used"})
        self.assertIn("Word", retained["aliases"])

    def test_published_wins_and_records_conflicts_in_either_order(self):
        declared = declarations.extract("int setter(int value);", {"kind": "declared"})
        published = declarations.extract("void setter(unsigned int value);", {"kind": "proven"})
        for seeds in ([declared, published], [published, declared]):
            graph = Constraints()
            record = _merge_records(seeds, "functions", graph)["setter"]
            self.assertEqual(record["return"], "void")
            self.assertFalse(record["declaration_conflict"])
            self.assertEqual(graph.facts[0]["kind"], "published_contract_conflict")

    def test_legacy_c_inventory_and_transitive_installed_contracts(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            src = root / "src"
            src.mkdir()
            include = root / "include"
            include.mkdir()
            (src / "owner.c").write_text(
                "struct Private { int x; }; void owner(void) { dispatch[record.index].callback(); }"
            )
            (src / "scratch.c").write_text("void scratch(void) { unused(); }")
            header = include / "types.h"
            header.write_text(
                "typedef unsigned int Word;\n"
                "typedef void (*Callback)(void);\n"
                "struct Dispatch { Callback callback; Word tail[2]; };\n"
                "extern struct Dispatch dispatch[];\n"
                "struct Record { Word index; };\nextern struct Record record;\n"
                "void unused(void);\nstruct Private { int x; };\n"
            )
            project = SimpleNamespace(root=root, src=src, include=(include,), versions=("us", "eu"))
            rows = [
                SimpleNamespace(kind="c", name="owner", path="owner"),
                SimpleNamespace(kind="asm", name="other", path="other"),
            ]
            with (
                patch("unbake.layout.split.functions", return_value=rows),
                patch("unbake.layout.index.headers", return_value=[header]),
            ):
                self.assertEqual(len(declarations.published_sources(project)), 2)
                components, homes = declaration_evidence.published_snapshot(project)
                self.assertTrue(all(paths == {header} for paths in homes.values()))
            retained = "".join(components.values())
            for contract in (
                "dispatch[]",
                "struct Dispatch",
                "(*Callback)",
                "typedef unsigned int Word",
                "struct Record",
                "record;",
            ):
                self.assertIn(contract, retained)
            self.assertNotIn("unused", retained)
            self.assertNotIn("struct Private", retained)

    def test_published_global_storage_survives_conflicting_value_flow(self):
        mapped = facts({"setter": (0x80001000, [0x3C088000, 0xAD044000, 0x03E00008, 0])})
        mapped["globals"] = {"cell": {"versions": {"us": {"address": 0x80004000}}}}
        mapped["functions"]["setter"]["versions"]["us"]["memory"][0]["symbols"] = ["cell"]
        seed = declarations.extract("extern int cell; void setter(unsigned int value);", {"kind": "published"})
        result = infer(SimpleNamespace(), mapped, [seed])
        self.assertEqual(result["globals"]["cell"]["state"], "known")
        self.assertEqual(result["globals"]["cell"]["type"], "int")
        self.assertEqual(result["globals"]["cell"]["declaration"], "extern int cell;")
        self.assertTrue(result["conflicts"])

    def test_published_array_contract_wins_over_ordinary_pointer_guess(self):
        declared = declarations.extract("extern void *table;", {"kind": "declared"})
        published = declarations.extract("extern void *table[4];", {"kind": "published"})
        for seeds in ([declared, published], [published, declared]):
            result = infer(SimpleNamespace(), facts({}), seeds)
            self.assertEqual(result["globals"]["table"]["state"], "known")
            self.assertEqual(result["globals"]["table"]["declaration"], "extern void *table[4];")
            self.assertEqual(result["arrays"]["table"]["extent"], "4")

    def test_all_callers_prevent_truncation_to_first_declared_signature(self):
        mapped = facts(
            {
                "caller1": (0x80001000, [0x24040001, 0x24050002, 0x0C000C00, 0x24060020, 0x03E00008, 0]),
                "caller2": (0x80002000, [0x24040003, 0x24050004, 0x0C000C00, 0x24060020, 0x03E00008, 0]),
                "allocate": (0x80003000, [0x00851021, 0x03E00008, 0]),
            }
        )
        seed = declarations.extract("int allocate(int a, int b);", {"kind": "declared"})
        result = infer(SimpleNamespace(), mapped, [seed])
        self.assertEqual(result["functions"]["allocate"]["arity"], 3)
        self.assertEqual(result["functions"]["allocate"]["params"][2]["register"], "r6")
        self.assertTrue(any(row["kind"] == "call_arity_conflict" for row in result["constraints"]))
        proven = declarations.extract("int allocate(int a, int b);", {"kind": "proven"})
        result = infer(SimpleNamespace(), mapped, [proven])
        self.assertEqual(result["functions"]["allocate"]["arity"], 2)
        self.assertTrue(any(row.get("resolution") == "published contract retained" for row in result["constraints"]))

    def test_abi_carrier_handles_extra_registers_in_conflicting_signature(self):
        from unbake.typemap.abi_declarations import prototype

        record = {
            "abi": {
                "registers": ["r4", "r5", "r6"],
                "used_returns": [],
                "return_known": True,
                "void": True,
                "conflicts": [],
                "missing": [],
            },
            "params": [{"register": "r4", "type": "int", "state": "known"}],
            "return": {"type": "void", "state": "known"},
        }
        self.assertEqual(prototype("callee", record, {})["prototype"], "void callee(int, int, int);")

    def test_array_storage_cannot_become_an_array_return_prototype(self):
        mapped = facts({"getter": (0x80001000, [0x24020001, 0x03E00008, 0])})
        mapped["functions"]["getter"]["versions"]["us"]["returns"][0]["values"]["r2"] = {
            "origins": [{"id": "global:table", "offset": 0}],
            "defined": True,
            "unknown": False,
        }
        mapped["globals"] = {"table": {"versions": {"us": {"address": 0x80003000}}, "accesses": []}}
        result = infer(
            SimpleNamespace(), mapped, [declarations.extract("extern float table[4];", {"kind": "published"})]
        )
        record = result["functions"]["getter"]
        self.assertIsNone(record["prototype"])
        self.assertEqual(record["return"]["state"], "unknown")
        self.assertNotIn("[4]", record["abi_declaration"]["prototype"] or "")

    def test_guarded_map_homes_survive_a_missing_generated_index(self):
        from unbake.layout import index
        from unbake.typemap import split

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            include = root / "include"
            (include / "main").mkdir(parents=True)
            (include / "common").mkdir()
            (root / "layout.toml").write_text('schema = 1\ncap = 32\n[[group]]\nname = "owner"\nsegment = "main"\n')
            home = include / "main/owner.h"
            home.write_bytes(split.guarded(Path("main/owner.h"), "struct Needed { int word; };"))
            shared = include / "common/types.h"
            shared.write_bytes(split.guarded(Path("common/types.h"), "typedef int Word;"))
            authored = include / "main/types.h"
            authored.write_text("typedef unsigned int Authored;\n")
            project = SimpleNamespace(root=root, build=root / "build", include=(include,))
            self.assertEqual(index.headers(project), {home, shared})
            self.assertNotIn(authored, index.headers(project))

    def test_pointer_to_array_return_uses_a_valid_C_declarator(self):
        from unbake.typemap.abi_declarations import prototype

        record = {
            "abi": {
                "registers": [],
                "return_register": "r2",
                "used_returns": [],
                "return_known": True,
                "void": False,
                "conflicts": [],
                "missing": [],
            },
            "params": [],
            "return": {"state": "known", "type": "int[4] *"},
        }
        carrier = prototype("getter", record, {})["prototype"]
        self.assertEqual(carrier, "int (*getter(void))[4];")
        declarations.extract(carrier, {})
