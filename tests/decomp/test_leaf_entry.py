"""Existing public fuzzy admission reconciles only proved stale leaf carriers."""

import copy
import hashlib
import json
import struct
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from unbake import land
from unbake.config import Held
from unbake.decomp import draft_abi
from unbake.typemap.mips import Analysis


def leaf_entry_record(*args):
    return draft_abi.leaf_entry_record(*args)


FIXTURE = Path(__file__).parents[1] / "fixtures/ragewars_leaf_entry"
NAME = "func_802BC558_de"
RECORD = json.loads((FIXTURE / "old-record.json").read_text())
PROGRAMS = json.loads((FIXTURE / "machine.json").read_text())["programs"][NAME]
SOURCE = "typedef struct ThreadNode { struct ThreadNode *next; } ThreadNode;\n" + "\n".join(
    line for line in (FIXTURE / "source.c").read_text().splitlines() if not line.startswith("#include")
)


def body(version):
    row = PROGRAMS[version]
    binary = bytes.fromhex(row["hex"])
    assert hashlib.sha256(binary).hexdigest() == row["target_sha256"]
    words = list(struct.unpack(">" + str(len(binary) // 4) + "I", binary))
    return {
        "target_sha256": row["target_sha256"],
        **Analysis(NAME, version, row["address"], row["rom_offset"], words, {}, {}).run(),
    }


class LeafEntryTests(unittest.TestCase):
    def test_real_all_holder_admission_reads_each_leaf_and_database_record_once(self):
        before = copy.deepcopy(RECORD)
        with (
            patch(
                "unbake.decomp.draft_abi.mapped_body", side_effect=lambda project, name, version: body(version)
            ) as mapped,
            patch("unbake.typemap.types_db.path", return_value=Path("types.sqlite")),
            patch("unbake.typemap.types_db.entries", return_value={NAME: RECORD}) as entries,
            patch("unbake.typemap.types_db.meta", return_value={}),
        ):
            for version in PROGRAMS:
                land._fuzzy_signature(SimpleNamespace(), NAME, version, SOURCE)
        self.assertEqual(mapped.call_count, 5)
        self.assertEqual(entries.call_count, 5)
        self.assertEqual(RECORD, before)
        self.assertEqual(RECORD["abi"]["registers"], ["r4", "r5", "r6"])

    def test_narrow_record_retains_semantic_conflicts_and_does_not_mutate_sqlite(self):
        current = leaf_entry_record(RECORD, NAME, "de", body("de"))
        self.assertFalse(current is RECORD)
        self.assertEqual(current["state"], "conflict")
        self.assertIsNone(current["prototype"])
        self.assertEqual(current["abi"]["registers"], ["r4"])
        self.assertFalse(current["abi"]["missing"])
        self.assertEqual(current["abi_declaration"]["prototype"], "int " + NAME + "(int);")
        self.assertEqual(current["entry_reconciliation"]["caller_only_registers"], ["r5", "r6"])
        self.assertTrue(current["entry_reconciliation"]["target_sha256"])
        self.assertTrue(RECORD["abi"]["missing"])

    def test_typed_contracts_missing_consumed_values_and_holder_disagreements_remain_strict(self):
        for change in (
            "known",
            "published",
            "proven",
            "missing",
            "version",
            "pair",
            "unknown_return",
            "fp_parameter",
            "fp_return",
            "conflict",
        ):
            record = copy.deepcopy(RECORD)
            if change == "known":
                record["prototype"] = "int " + NAME + "(int,int,int);"
            elif change in ("published", "proven"):
                record["provenance"] = [{"kind": change, "function": NAME}]
            elif change == "missing":
                record["abi"]["missing"].append({"register": "r4"})
            elif change == "version":
                record["abi"]["inputs"]["eu"].append("r5")
            elif change == "pair":
                record["abi"]["return_width"] = 8
            elif change == "unknown_return":
                record["abi"]["return_known"] = False
            elif change == "fp_parameter":
                record["params"][0]["type"] = "float"
            elif change == "fp_return":
                record["return"]["type"] = "float"
            else:
                record["abi"]["conflicts"] = ["callee input registers differ across versions"]
            with self.subTest(change=change):
                self.assertIs(leaf_entry_record(record, NAME, "de", body("de")), record)

    def test_forwarding_incomplete_or_unpinned_bodies_cannot_trim_a_carrier(self):
        for change in ("call", "unknown", "sha", "input", "exit"):
            current = body("de")
            if change == "call":
                current["calls"] = [{"callee": "forwarded"}]
            elif change == "unknown":
                current["unknown"] = ["unresolved control target"]
            elif change == "sha":
                del current["target_sha256"]
            elif change == "input":
                current["register_inputs"].append("r5")
            else:
                current["returns"][0]["values"]["r2"]["defined"] = False
            with self.subTest(change=change):
                self.assertIs(leaf_entry_record(RECORD, NAME, "de", current), RECORD)

    def test_proved_one_word_entry_does_not_admit_an_invented_extra_formal(self):
        with (
            patch("unbake.decomp.draft_abi.mapped_body", return_value=body("de")),
            patch("unbake.typemap.types_db.path", return_value=Path("types.sqlite")),
            patch("unbake.typemap.types_db.entries", return_value={NAME: RECORD}),
            patch("unbake.typemap.types_db.meta", return_value={}),
            self.assertRaisesRegex(Held, "land.fuzzy_abi"),
        ):
            land._fuzzy_signature(SimpleNamespace(), NAME, "de", "int " + NAME + "(int a, int b) {return b;}")
