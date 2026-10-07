"""ROUTE20 uses existing C no-result authority, never the candidate's void spelling."""

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

FIXTURE = Path(__file__).parents[1] / "fixtures/ragewars_declared_void"
NAME = "func_80226C60_de"
CALLEE = "func_80274098_de"
RECORDS = json.loads((FIXTURE / "records.json").read_text())
SOURCE = """
typedef float f32;
typedef struct { float x, y, z, w; } Vector4f;
typedef Vector4f Shared_Quad;
typedef struct SharedPlayer SharedPlayer_func_8020FDB0_de;
typedef struct View func_8022DBD4_S1;
""" + "\n".join(line for line in (FIXTURE / "source.c").read_text().splitlines() if not line.startswith("#include"))


class DeclaredVoidExitTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        raw = (FIXTURE / "machine.json").read_text()
        cls.functions = {}
        cls.byte_count = 0
        run = Analysis.run
        with (
            patch("json.loads", wraps=json.loads) as parse,
            patch.object(Analysis, "run", autospec=True, side_effect=run) as analyze,
        ):
            fixture = json.loads(raw)
            for name, versions in fixture["programs"].items():
                cls.functions[name] = {"versions": {}}
                for version, row in versions.items():
                    binary = bytes.fromhex(row["hex"])
                    assert hashlib.sha256(binary).hexdigest() == row["target_sha256"]
                    cls.byte_count += len(binary)
                    words = [word for (word,) in struct.iter_unpack(">I", binary)]
                    body = Analysis(
                        name,
                        version,
                        row["address"],
                        row["rom_offset"],
                        words,
                        {int(a): n for a, n in row["targets"].items()},
                        {},
                    ).run()
                    cls.functions[name]["versions"][version] = {
                        "target_sha256": row["target_sha256"],
                        "address": row["address"],
                        **body,
                    }
        cls.parse_count, cls.analysis_count = parse.call_count, analyze.call_count
        cls.expected_bytes, cls.expected_bodies = fixture["bytes"], fixture["body_count"]

    def body(self, version="de"):
        return copy.deepcopy(self.functions[NAME]["versions"][version])

    def admits(self, record=None, body=None, callee=None, version="de"):
        return draft_abi.declared_void_exit(
            RECORDS[NAME] if record is None else record,
            NAME,
            version,
            self.body(version) if body is None else body,
            RECORDS[CALLEE] if callee is None else callee,
        )

    def test_real_work_is_one_parse_thirty_bodies_and_11300_bytes(self):
        self.assertEqual(self.parse_count, 1)
        self.assertEqual(self.analysis_count, self.expected_bodies)
        self.assertEqual(self.analysis_count, 30)
        self.assertEqual(self.byte_count, self.expected_bytes)
        self.assertEqual(self.byte_count, 11300)
        self.assertEqual(sum(len(b["calls"]) for b in self.functions[NAME]["versions"].values()), 45)
        self.assertEqual(
            sum(c["callee"] == CALLEE for b in self.functions[NAME]["versions"].values() for c in b["calls"]), 15
        )
        incoming = [
            c
            for name, item in self.functions.items()
            if name != NAME
            for b in item["versions"].values()
            for c in b["calls"]
            if c["callee"] == NAME
        ]
        self.assertEqual(len(incoming), 10)
        self.assertTrue(all(not any(c["return_register_use"].values()) for c in incoming))

    def test_all_holders_use_declared_void_and_proved_last_callee_without_mutation(self):
        before = copy.deepcopy(RECORDS)
        for version in RECORDS[NAME]["versions"]:
            body = self.body(version)
            self.assertTrue(self.admits(body=body, version=version))
            self.assertEqual(body["calls"][-1]["callee"], CALLEE)
            self.assertEqual(len(body["calls"]), 9)
        self.assertEqual(RECORDS, before)
        self.assertFalse(RECORDS[NAME]["abi"]["return_known"])
        self.assertEqual(RECORDS[NAME]["params"][0]["state"], "conflict")
        self.assertEqual(RECORDS[NAME]["params"][0]["component"], "address:D_002018A0")

    def test_public_gate_reads_selected_body_and_two_records_once_per_holder(self):
        records = copy.deepcopy(RECORDS)
        queries = []
        project = SimpleNamespace(compiler_for=lambda name: SimpleNamespace(id="gcc-2.7.2-kmc"))

        def lookup(path, kind, names):
            queries.append(tuple(names))
            return {n: records[n] for n in names}

        with (
            patch.object(draft_abi, "mapped_body", side_effect=lambda p, n, v: self.body(v)) as bodies,
            patch("unbake.typemap.types_db.path", return_value=Path("types.sqlite")),
            patch("unbake.typemap.types_db.entries", side_effect=lookup) as reads,
            patch("unbake.typemap.types_db.meta", return_value={}),
        ):
            for version in RECORDS[NAME]["versions"]:
                land._fuzzy_signature(project, NAME, version, SOURCE)
        self.assertEqual(bodies.call_count, 5)
        self.assertEqual(reads.call_count, 10)
        self.assertEqual(queries, [(NAME,), (CALLEE,)] * 5)
        self.assertEqual(records, RECORDS)

    def test_a_candidate_void_definition_is_not_contract_authority(self):
        for returned in (
            {"state": "unknown", "type": None},
            {"state": "known", "type": "int"},
            {"state": "known", "type": "void", "provenance": []},
        ):
            record = copy.deepcopy(RECORDS[NAME])
            record["return"] = returned
            self.assertFalse(self.admits(record=record))
        record = copy.deepcopy(RECORDS[NAME])
        record["abi_declaration"]["prototype"] = "int " + NAME + "(int, void *);"
        self.assertFalse(self.admits(record=record))

    def test_missing_inputs_holder_differences_fp_pair_stack_and_return_reads_stay_held(self):
        for field, value in (
            ("arity_known", False),
            ("missing", [{"register": "r5"}]),
            ("conflicts", ["entry differs"]),
            ("return_width", 8),
            ("used_returns", ["f0"]),
            ("unproven_return_reads", ["r2"]),
            ("caller_return_uses", {"caller": ["r3"]}),
            ("registers", ["r4", "r5", "stack16"]),
        ):
            record = copy.deepcopy(RECORDS[NAME])
            record["abi"][field] = value
            with self.subTest(field=field):
                self.assertFalse(self.admits(record=record))
        for edit in (
            lambda r: r["abi"]["inputs"].pop("eu"),
            lambda r: r["abi"]["inputs"].__setitem__("eu", ["r4", "r5", "stack16"]),
        ):
            record = copy.deepcopy(RECORDS[NAME])
            edit(record)
            self.assertFalse(self.admits(record=record))
        for formals, registers in (
            ("float, void *", ["f12", "r5"]),
            ("long long, void *", ["r4", "r5", "r6"]),
            ("int, void *, int, int, int", ["r4", "r5", "r6", "r7", "stack16"]),
            ("int, void *, ...", ["r4", "r5"]),
        ):
            record = copy.deepcopy(RECORDS[NAME])
            record["abi_declaration"]["prototype"] = f"void {NAME}({formals});"
            record["abi"]["registers"] = registers
            self.assertFalse(self.admits(record=record))

    def test_unknown_or_foreign_callee_and_early_or_computed_exits_stay_held(self):
        for field, value in (
            ("state", "unknown"),
            ("prototype", "int " + CALLEE + "(void *, void *, void *);"),
            ("provenance", [{"kind": "proven", "function": "different"}]),
            ("provenance", [{"kind": "published", "function": CALLEE}]),
            ("versions", ["eu"]),
        ):
            callee = copy.deepcopy(RECORDS[CALLEE])
            callee[field] = value
            with self.subTest(field=field):
                self.assertFalse(self.admits(callee=callee))
        for field, value in (
            ("missing", [{"register": "r6"}]),
            ("conflicts", ["callee differs"]),
            ("used_returns", ["f0"]),
            ("unproven_return_reads", ["r2"]),
            ("return_width", 8),
        ):
            callee = copy.deepcopy(RECORDS[CALLEE])
            callee["abi"][field] = value
            self.assertFalse(self.admits(callee=callee))
        for edit in (
            lambda b: b.pop("target_sha256"),
            lambda b: b.__setitem__("unknown", [{"reason": "indirect jump"}]),
            lambda b: b["calls"][-1].__setitem__("tail", True),
            lambda b: b["calls"][-1]["return_register_use"].__setitem__("r2", [1]),
            lambda b: b["returns"][0].__setitem__("instruction", b["calls"][-1]["instruction"] - 4),
            lambda b: b["returns"][0]["values"]["r2"].__setitem__("constant", 0),
            lambda b: b["returns"][0]["values"]["f0"]["origins"][0].__setitem__("offset", 4),
            lambda b: b["returns"][0]["values"]["r2"]["dependencies"].append("return:other:de:0:r2"),
        ):
            body = self.body()
            edit(body)
            self.assertFalse(self.admits(body=body))

    def test_new_int_fp_pair_or_extra_formal_is_not_admitted_by_void_evidence(self):
        project = SimpleNamespace(compiler_for=lambda name: SimpleNamespace(id="gcc-2.7.2-kmc"))
        for changed in (
            SOURCE.replace("void " + NAME, "int " + NAME),
            SOURCE.replace("void " + NAME, "float " + NAME),
            SOURCE.replace("void " + NAME, "long long " + NAME),
            SOURCE.replace("Vector4f *output)", "Vector4f *output, int extra)"),
        ):
            with (
                patch.object(draft_abi, "mapped_body", return_value=self.body()),
                patch("unbake.typemap.types_db.path", return_value=Path("types.sqlite")),
                patch("unbake.typemap.types_db.entries", side_effect=lambda p, k, ns: {n: RECORDS[n] for n in ns}),
                patch("unbake.typemap.types_db.meta", return_value={}),
                self.assertRaisesRegex(Held, "definition differs from canonical"),
            ):
                land._fuzzy_signature(project, NAME, "de", changed)
