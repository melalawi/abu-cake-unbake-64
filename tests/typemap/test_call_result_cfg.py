"""Full BT callers discard the list-link result across their actual branch edges."""

import json
from pathlib import Path
from unittest.mock import patch

from tests.project_fixture import ProjectCase
from tests.typemap.test_solver import facts
from unbake import config
from unbake.typemap import database, evidence, regeneration
from unbake.typemap.mips import Analysis

FIXTURE = Path(__file__).parent / "fixtures/battletanx_call_cfg"
PAYLOAD = json.loads((FIXTURE / "machine.json").read_text())
CALLEE = "func_8011C180"


def real_facts():
    targets = {int(address): name for address, name in PAYLOAD["targets"].items()}
    return {
        name: {
            "versions": {
                "us": {
                    "address": row["address"],
                    **Analysis(
                        name,
                        "us",
                        row["address"],
                        row["rom_offset"],
                        [int(word, 16) for word in row["words"]],
                        targets,
                        {},
                    ).run(),
                }
            }
        }
        for name, row in PAYLOAD["programs"].items()
    }


class CallResultCFGTests(ProjectCase):
    versions = ("us",)

    def test_full_real_callers_skip_all_alleged_primary_and_secondary_result_reads(self):
        run = Analysis.run
        with patch.object(Analysis, "run", autospec=True, side_effect=run) as analyzed:
            machine = real_facts()
        self.assertEqual(analyzed.call_count, 3)
        self.assertEqual(sum(len(row["words"]) for row in PAYLOAD["programs"].values()), 124)
        calls = [
            call for name, row in machine.items() for call in row["versions"]["us"]["calls"] if call["callee"] == CALLEE
        ]
        self.assertEqual([call["instruction"] for call in calls], [0x80119F3C, 0x8011B6C8, 0x8011B6FC])
        self.assertTrue(all(not uses for call in calls for uses in call["return_register_use"].values()))
        abi = evidence.abi(machine)[CALLEE]
        self.assertEqual(abi["used_returns"], [])
        self.assertEqual(abi["unproven_return_reads"], [])
        self.assertIsNone(abi["return_width"])
        self.assertFalse(abi["return_pair_known"])
        self.assertTrue(abi["return_known"])
        self.assertEqual(abi["return_register"], "r2")

    def test_actual_default_regeneration_keeps_the_published_void_definition(self):
        row = self.project.version("us")
        for path in (row.split, row.symbols, self.project.root / "layout.toml"):
            path.write_text(path.read_text().replace("alpha", CALLEE).replace("asm, " + CALLEE, "c, " + CALLEE))
        self.project = config.load(self.project.root)
        source = (FIXTURE / "published-owner.c").read_text()
        (self.project.src / (CALLEE + ".c")).write_text(source)
        header = self.project.include[0] / "span_1000/code_8011C150.h"
        header.parent.mkdir(parents=True)
        header.write_text(
            "struct Shape_func_8011C150; typedef struct Shape_func_8011C150 Shape_func_8011C150;\n"
            "/* unbake published declaration: published_owner */\n"
            f"extern void {CALLEE}(Shape_func_8011C150 *node, Shape_func_8011C150 *list);\n"
        )
        value = {kind: {} for kind in ("functions", "globals", "structs", "arrays")}
        value["functions"][CALLEE] = {
            "state": "known",
            "prototype": f"int {CALLEE}(int, int);",
            "abi": evidence.abi(real_facts())[CALLEE],
            "provenance": [],
        }
        with patch("unbake.runner.compile_unit", side_effect=AssertionError("native replay")) as native:
            session = regeneration.Session(self.project, None)
            outputs = database._render(self.project, value, None, session)
        effective = {path: path.read_bytes() for path in self.project.include[0].rglob("*.h")}
        effective.update({path: data for path, data in outputs.items() if path.suffix == ".h"})
        rendered = "\n".join(data.decode() for data in effective.values())
        self.assertIn(f"void {CALLEE}(", rendered)
        self.assertNotIn(f"int {CALLEE}(", rendered)
        self.assertEqual(native.call_count, 0)
        self.assertEqual((self.project.src / (CALLEE + ".c")).read_text(), source)

    def test_undefined_secondary_read_is_preserved_without_inventing_a_primary_or_pair(self):
        words = [int(word, 16) for word in PAYLOAD["programs"][CALLEE]["words"]]
        machine = facts(
            {CALLEE: (0x80002000, words), "caller": (0x80001000, [0x0C000800, 0, 0x00604025, 0x03E00008, 0])}
        )
        abi = evidence.abi(machine["functions"])[CALLEE]
        self.assertEqual(abi["used_returns"], [])
        self.assertEqual(abi["unproven_return_reads"], ["r3"])
        self.assertIsNone(abi["return_width"])
        self.assertFalse(abi["return_known"])
        self.assertEqual(abi["caller_return_uses"], {"caller": ["r3"]})

    def test_ordinary_slot_consumes_a_result_but_annulled_likely_slot_does_not(self):
        for branch, expected in ((0x10000001, [0x8000100C]), (0x14000001, [0x8000100C]), (0x54000001, [])):
            with self.subTest(branch=hex(branch)):
                body = Analysis(
                    "caller",
                    "us",
                    0x80001000,
                    0,
                    [0x0C000800, 0, branch, 0xAC820000, 0x03E00008, 0],
                    {0x80002000: "leaf"},
                    {},
                ).run()
                self.assertEqual(body["calls"][0]["return_register_use"]["r2"], expected)

    def test_unknown_condition_retains_feasible_consumption_and_defined_pairs_stay_pairs(self):
        machine = facts(
            {
                "leaf": (0x80002000, [0x24020001, 0x24030002, 0x03E00008, 0]),
                "caller": (0x80001000, [0x0C000800, 0, 0x10800003, 0, 0xAC820000, 0xAC830004, 0x03E00008, 0]),
            }
        )
        abi = evidence.abi(machine["functions"])["leaf"]
        self.assertEqual(abi["used_returns"], ["r2"])
        self.assertEqual(abi["return_width"], 8)
        self.assertTrue(abi["return_pair_known"])

    def test_signed_constant_branches_and_conditional_call_annulment_preserve_only_feasible_paths(self):
        # li a0,-1; bgezall a0,leaf (not taken); annulled slot reads v0.
        body = Analysis(
            "caller", "us", 0x80001000, 0, [0x2404FFFF, 0x049301FE, 0x00404025, 0x03E00008, 0], {0x80001800: "leaf"}, {}
        ).run()
        self.assertEqual(body["calls"], [])
        self.assertNotIn("r2", body["register_inputs"])
        # li t0,7; li t1,7; beq t0,t1 skips a read of the live call result.
        body = Analysis(
            "caller",
            "us",
            0x80001000,
            0,
            [0x0C000800, 0, 0x24080007, 0x24090007, 0x11090002, 0, 0x00404025, 0x03E00008, 0],
            {0x80002000: "leaf"},
            {},
        ).run()
        self.assertEqual(body["calls"][0]["return_register_use"]["r2"], [])

    def test_comparing_a_result_with_itself_does_not_claim_consumption(self):
        body = Analysis(
            "caller", "us", 0x80001000, 0, [0x0C000800, 0, 0x10420001, 0, 0x03E00008, 0], {0x80002000: "leaf"}, {}
        ).run()
        self.assertEqual(body["calls"][0]["return_register_use"]["r2"], [])
