"""Route 6: real paired timer results and authored graphics pointer contracts."""

import copy
import json
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from tests.typemap.test_solver import facts, solve
from unbake import land
from unbake.config import Held
from unbake.decomp import draft_abi
from unbake.typemap import declarations, evidence, o32
from unbake.typemap.mips import Analysis
from unbake.typemap.solver import infer

FIXTURE = Path(__file__).parents[1] / "fixtures/battletanx_pair_abi"
TIMER = "func_801120A0_us"
ALIASES = "typedef unsigned long long u64; typedef union Gfx Gfx;\n"


def real_facts():
    data = json.loads((FIXTURE / "machine.json").read_text())["programs"]
    targets = {p["address"]: name for name, p in data.items()}
    functions = {}
    for name, p in data.items():
        body = Analysis(name, "us", p["address"], p["rom_offset"], [int(w, 16) for w in p["words"]], targets, {}).run()
        functions[name] = {"versions": {"us": {"address": p["address"], **body}}}
    return {"functions": functions, "globals": {}}


class PairAbiTests(unittest.TestCase):
    def admit(self, record, source):
        with (
            patch("unbake.decomp.draft_abi.mapped_body", return_value=object()),
            patch("unbake.typemap.types_db.path", return_value=Path("unused.sqlite")),
            patch("unbake.typemap.types_db.entries", return_value={TIMER: record}),
            patch("unbake.typemap.types_db.meta", return_value={}),
        ):
            land._fuzzy_signature(SimpleNamespace(), TIMER, "us", source)

    def test_real_timer_carrier_preserves_both_words_and_semantic_uncertainty(self):
        record = infer(SimpleNamespace(), real_facts(), [])["functions"][TIMER]
        self.assertEqual(record["abi"]["return_width"], 8)
        self.assertTrue(record["abi"]["return_pair_known"])
        self.assertTrue(record["abi"]["return_known"])
        self.assertIsNone(record["prototype"])
        self.assertEqual(record["abi_declaration"]["prototype"], f"unsigned long long {TIMER}();")
        self.assertIn("types.abi.pair_return", str(record["abi_declaration"]["reasons"]))
        self.admit(record, ALIASES + f"u64 {TIMER}(void) {{return 0;}}")
        for type_ in ("int", "float", "void *"):
            with self.subTest(type=type_), self.assertRaisesRegex(Held, "land.fuzzy_abi"):
                self.admit(record, f"{type_} {TIMER}(void) {{return 0;}}")
        with (
            patch("unbake.typemap.types_db.entries", return_value={TIMER: record}),
            patch("unbake.layout.split.functions", return_value=[]),
        ):
            text = draft_abi.declarations(
                SimpleNamespace(), SimpleNamespace(), "us", "jal " + TIMER, "", types_path=Path("unused.sqlite")
            )
        self.assertIn(f"unsigned long long {TIMER}(void);", text)

    def test_authored_real_pointer_and_pair_contracts_survive_inference(self):
        source = ALIASES + (FIXTURE / "menu_render.h").read_text()
        seed = declarations.extract(source, {"kind": "declared", "source": "include/menu_render.h"})
        result = infer(SimpleNamespace(), real_facts(), [seed])["functions"]
        self.assertEqual(result[TIMER]["prototype"], f"u64 {TIMER}(void);")
        for name in ("func_800F4DFC", "func_80105A50"):
            record = result[name]
            prototype = record["prototype"] or record["abi_declaration"]["prototype"]
            self.assertIn(name + "(Gfx **", prototype)
            self.assertTrue(o32.compatible_prototypes(prototype, seed["functions"][name]["prototype"], seed["aliases"]))
        self.assertEqual(result["func_800F4DFC"]["arity"], 9)
        self.assertEqual(result["func_80105A50"]["arity"], 4)

    def test_incidental_v1_without_demand_does_not_invent_a_pair(self):
        record = solve({"leaf": (0x80001000, [0x24020001, 0x24030002, 0x03E00008, 0])}, "")["functions"]["leaf"]
        self.assertIsNone(record["abi"]["return_width"])
        self.assertEqual(record["prototype"], "int leaf(void);")

    def test_pair_demand_and_forwarding_propagate_through_epilogues(self):
        programs = {
            "caller": (0x80001000, [0x0C000800, 0, 0xAC820000, 0xAC830004, 0x03E00008, 0]),
            "wrapper": (0x80002000, [0x0C000C00, 0, 0x03E00008, 0]),
            "leaf": (0x80003000, [0x24020001, 0x24030002, 0x03E00008, 0]),
        }
        result = evidence.abi(facts(programs)["functions"])
        for name in ("wrapper", "leaf"):
            self.assertEqual(result[name]["return_width"], 8)
            self.assertTrue(result[name]["return_pair_known"])
            self.assertTrue(result[name]["return_known"])

    def test_authored_pair_requires_low_word_at_every_exit(self):
        for words in (
            [0x24020001, 0x03E00008, 0],
            [0x10800004, 0, 0x24030002, 0x10000001, 0, 0x24020001, 0x03E00008, 0],
        ):
            result = solve({TIMER: (0x80001000, words)}, f"unsigned long long {TIMER}(void);")
            record = result["functions"][TIMER]
            self.assertFalse(record["abi"]["return_pair_known"])
            self.assertNotEqual(record["state"], "known")
            with self.subTest(words=words), self.assertRaisesRegex(Held, "return pair"):
                self.admit(record, f"unsigned long long {TIMER}(void) {{return 0;}}")

    def test_secondary_return_demand_cannot_be_hidden_in_an_abi_carrier(self):
        record = infer(SimpleNamespace(), real_facts(), [])["functions"][TIMER]
        record = copy.deepcopy(record)
        record["abi"].update(return_pair_known=False)
        with self.assertRaisesRegex(Held, "return pair"):
            self.admit(record, ALIASES + f"u64 {TIMER}(void) {{return 0;}}")
