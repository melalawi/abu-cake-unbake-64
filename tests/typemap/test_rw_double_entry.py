"""Explicit aligned double grouping covers both real GPR input words."""

import hashlib
import json
import struct
import unittest
from collections import UserDict
from pathlib import Path
from types import SimpleNamespace

from unbake.typemap import declarations, o32
from unbake.typemap.mips import Analysis
from unbake.typemap.solver import infer

FIXTURE = Path(__file__).parents[1] / "fixtures/ragewars_double_entry"
NAME = "func_80414400_de"
CONTRACT = (FIXTURE / "contract.h").read_text()
PROGRAMS = json.loads((FIXTURE / "machine.json").read_text())["programs"]


def facts():
    versions = {}
    for version, row in PROGRAMS[NAME].items():
        binary = bytes.fromhex(row["hex"])
        assert hashlib.sha256(binary).hexdigest() == row["target_sha256"]
        words = list(struct.unpack(">" + str(len(binary) // 4) + "I", binary))
        targets = {int(k): v for k, v in row["targets"].items()}
        versions[version] = {
            "address": row["address"],
            **Analysis(NAME, version, row["address"], row["rom_offset"], words, targets, {}).run(),
        }
    return {"functions": {NAME: {"aliases": [], "versions": versions}}, "globals": {}}


class RwDoubleEntryTests(unittest.TestCase):
    def test_real_word_pair_is_one_declared_double_without_false_arity_conflict(self):
        machine = facts()
        counts = {}

        class Reads(UserDict):
            def __init__(self, rows):
                super().__init__(rows)
                self.inventory = {
                    NAME: {"versions": {v: {"address": row["address"]} for v, row in rows[NAME]["versions"].items()}}
                }

            def __getitem__(self, name):
                counts[name] = counts.get(name, 0) + 1
                return super().__getitem__(name)

        machine["functions"] = Reads(machine["functions"])
        seed = declarations.extract(CONTRACT, {"kind": "published"})
        result = infer(SimpleNamespace(), machine, [seed])
        self.assertFalse(
            [
                row
                for row in result["constraints"]
                if row.get("kind") == "call_arity_conflict" and row.get("entity") == "functions:" + NAME
            ]
        )
        record = result["functions"][NAME]
        self.assertEqual(record["arity"], 5)
        self.assertEqual([p["register"] for p in record["params"]], ["r4", "r6", "stack16", "stack20", "stack24"])
        self.assertEqual(record["argument_words"], ["r4", "r6", "r7", "stack16", "stack20", "stack24"])
        self.assertNotIn("r5", record["argument_words"])
        self.assertEqual(record["versions"], ["de", "eu", "eu-x", "us", "us-rev1"])
        self.assertEqual(counts, {NAME: 3})

    def test_pair_footprint_keeps_scalar_and_prefix_conventions_distinct(self):
        seed = declarations.extract(CONTRACT, {})
        self.assertEqual(
            o32.argument_words(seed["functions"][NAME], seed["aliases"]),
            {"r4", "r6", "r7", "stack16", "stack20", "stack24"},
        )
        for text, expected in (
            ("void f(double, int);", {"f12", "r6"}),
            ("void f(int, float);", {"r4", "r5"}),
            ("void f(int, long long);", {"r4", "r6", "r7"}),
            ("void f(int,int,int,int,double);", {"r4", "r5", "r6", "r7", "stack16", "stack20"}),
        ):
            seed = declarations.extract(text, {})
            with self.subTest(source=text):
                self.assertEqual(o32.argument_words(seed["functions"]["f"], {}), expected)
