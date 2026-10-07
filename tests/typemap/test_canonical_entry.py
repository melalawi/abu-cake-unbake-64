"""Canonical entry transport from real BattleTanx and RageWars payload slices."""

import copy
import hashlib
import json
import struct
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from tests.typemap.test_solver import solve
from unbake import land
from unbake.config import Held
from unbake.decomp import draft_abi
from unbake.typemap import declarations, evidence
from unbake.typemap.mips import Analysis
from unbake.typemap.solver import infer

FIXTURE = Path(__file__).parents[1] / "fixtures/canonical_entry"
PROGRAMS = json.loads((FIXTURE / "machine.json").read_text())["programs"]


def real_facts(names):
    targets = {p["address"]: name for name, p in PROGRAMS.items()}
    # The real RW call target has a published one-word void contract.
    targets[0x8041B190] = "func_8041B110_de"
    functions = {}
    for name in names:
        p = PROGRAMS[name]
        words = [int(word, 16) for word in p["words"]]
        assert hashlib.sha256(b"".join(struct.pack(">I", word) for word in words)).hexdigest() == p["target_sha256"]
        body = Analysis(name, p["version"], p["address"], p["rom_offset"], words, targets, {}).run()
        functions[name] = {"versions": {p["version"]: {"address": p["address"], **body}}}
    return {"functions": functions, "globals": {}}


def real_record(name):
    seeds = [declarations.extract("void func_8041B110_de(int);", {"kind": "published"})] if "80429178" in name else []
    return infer(SimpleNamespace(), real_facts([name]), seeds)["functions"][name]


class CanonicalEntryTests(unittest.TestCase):
    def admit(self, name, record, source):
        with (
            patch("unbake.decomp.draft_abi.mapped_body", return_value=object()),
            patch("unbake.typemap.types_db.path", return_value=Path("unused.sqlite")),
            patch("unbake.typemap.types_db.entries", return_value={name: record}),
            patch("unbake.typemap.types_db.meta", return_value={}),
        ):
            land._fuzzy_signature(SimpleNamespace(), name, PROGRAMS[name]["version"], source)

    def test_real_bt_partial_temporaries_allow_only_void_transport(self):
        name = "func_800D1BA0_us"
        record = real_record(name)
        self.assertFalse(record["abi"]["return_known"])
        self.assertTrue(record["abi"]["discardable_return"])
        self.assertEqual(record["abi_declaration"]["prototype"], f"void {name}(int);")
        self.assertIsNone(record["prototype"])
        source = (FIXTURE / (name + ".c")).read_text()
        self.admit(name, record, source)
        for bad in (f"int {name}(int flag) {{return flag;}}", f"void {name}(float flag) {{}}"):
            with self.subTest(source=bad), self.assertRaisesRegex(Held, "land.fuzzy_abi"):
                self.admit(name, record, bad)

    def test_real_bt_result_demand_stays_a_contradiction(self):
        name = "func_800D1BA0_us"
        abi = evidence.abi(real_facts([name])["functions"], {name: "r2"})[name]
        self.assertFalse(abi["discardable_return"])
        self.assertFalse(abi["return_known"])
        self.assertIn("callers consume return registers not defined", str(abi["conflicts"]))

    def test_real_rw_discarded_load_keeps_facts_without_an_argument(self):
        name = "func_80429178_us_rev1"
        facts = real_facts([name])
        body = facts["functions"][name]["versions"]["us-rev1"]
        self.assertEqual(body["memory"][0]["offset"], 80)
        self.assertEqual(body["memory"][0]["loaded"]["origins"][0]["id"], f"param:{name}:stack80")
        record = real_record(name)
        self.assertEqual(record["abi"]["registers"], ["r4"])
        self.assertTrue(record["abi"]["arity_known"])
        self.assertEqual(record["abi_declaration"]["prototype"], f"int {name}(int);")
        self.admit(name, record, (FIXTURE / (name + ".c")).read_text())
        for bad in (
            f"int {name}(int first, int second) {{return second;}}",
            f"int {name}(int first, double unused) {{return 0;}}",
            f"int {name}(int first, float unused) {{return 0;}}",
            f"int {name}(int first, ...) {{return 0;}}",
        ):
            with self.subTest(source=bad), self.assertRaisesRegex(Held, "land.fuzzy_abi"):
                self.admit(name, record, bad)

    def test_known_contracts_and_missing_caller_evidence_stay_strict(self):
        name = "func_80429178_us_rev1"
        source = (FIXTURE / (name + ".c")).read_text()
        for change in ("known", "missing", "conflict"):
            record = copy.deepcopy(real_record(name))
            if change == "known":
                record.update(state="known", prototype=f"int {name}(int);")
            elif change == "missing":
                record["abi"]["missing"] = [{"register": "r4"}]
            else:
                record["abi"]["conflicts"] = ["callee input registers differ across versions"]
            with self.subTest(change=change), self.assertRaisesRegex(Held, "land.fuzzy_abi"):
                self.admit(name, record, source)

    def test_stack_values_used_or_forwarded_remain_arguments(self):
        for words in ([0x8FA20050, 0x03E00008, 0], [0x8FA80050, 0xAD080000, 0x03E00008, 0]):
            record = solve({"leaf": (0x80001000, words)}, "")["functions"]["leaf"]
            self.assertIn("stack80", record["abi"]["registers"])
        record = solve(
            {
                "caller": (0x80001000, [0x8FA40050, 0x0C000800, 0, 0x03E00008, 0]),
                "leaf": (0x80002000, [0x8C820000, 0x03E00008, 0]),
            },
            "",
        )["functions"]["caller"]
        self.assertIn("stack80", record["abi"]["registers"])

    def test_unresolved_forwarded_or_ambiguous_results_do_not_become_void(self):
        for programs in (
            {"leaf": (0x80001000, [0x0C000800, 0, 0x03E00008, 0])},
            {"leaf": (0x80001000, [0x24020001, 0x44800000, 0x03E00008, 0])},
        ):
            record = solve(programs, "")["functions"]["leaf"]
            self.assertFalse(record["abi"]["discardable_return"])

    def test_real_library_inputs_come_from_callee_not_leftover_callers(self):
        names = ["func_801254A0_us", "func_801120A0_us", "func_800A03B8", "func_80079064_us"]
        abi = evidence.abi(real_facts(names)["functions"])
        self.assertEqual(abi[names[0]]["registers"], ["r4", "r5"])
        self.assertEqual(abi[names[1]]["registers"], [])
        self.assertEqual(abi[names[2]]["registers"], ["r4"])
        self.assertEqual(abi[names[3]]["registers"], [])

    def test_real_unused_call_registers_cannot_reinvent_draft_parameters(self):
        name = "func_80079064_us"
        record = json.loads((FIXTURE / "unused_call.json").read_text())
        self.assertTrue(record["abi"]["caller_arguments"])
        self.assertFalse(record["abi"]["registers"])
        with (
            patch("unbake.typemap.types_db.entries", return_value={name: record}),
            patch("unbake.layout.split.functions", return_value=[]),
        ):
            text = draft_abi.declarations(
                SimpleNamespace(), SimpleNamespace(), "us", "jal " + name, "", types_path=Path("unused.sqlite")
            )
        self.assertIn(name + "(void)", text)
        self.assertEqual(record["abi_declaration"]["prototype"], f"void {name}();")
