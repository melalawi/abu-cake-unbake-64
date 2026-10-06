"""Landing a C rendering changes storage classification without changing machine evidence."""

import copy
import unittest

from unbake.typemap import solver


class MachineKeyTests(unittest.TestCase):
    def setUp(self):
        self.facts = {"shard_sha256": "rom facts", "globals": {}, "abi_supplement": None}
        self.inventory = {
            "f": {
                "aliases": ["f"],
                "versions": {
                    "us": {"kind": "asm", "address": 4, "start": 8, "end": 16, "target_sha256": "body", "name": "f"}
                },
            }
        }

    def test_published_storage_does_not_invalidate_abi_or_machine_graph(self):
        before = solver._map_parts(self.facts, self.inventory)
        self.inventory["f"]["versions"]["us"]["kind"] = "c"
        self.assertEqual(solver._map_parts(self.facts, self.inventory), before)

    def test_placements_aliases_bodies_and_global_evidence_remain_inputs(self):
        before = solver._map_parts(self.facts, self.inventory)
        for field in ("address", "start", "end", "target_sha256", "name"):
            inventory = copy.deepcopy(self.inventory)
            inventory["f"]["versions"]["us"][field] = "different"
            self.assertNotEqual(solver._map_parts(self.facts, inventory), before)
        inventory = copy.deepcopy(self.inventory)
        inventory["f"]["aliases"].append("alias")
        self.assertNotEqual(solver._map_parts(self.facts, inventory), before)
        facts = {**self.facts, "globals": {"x": {"versions": {"us": {"address": 20}}}}}
        self.assertNotEqual(solver._map_parts(facts, self.inventory), before)
