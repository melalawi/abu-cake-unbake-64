"""Whole real RW texture dispatch, including the ninth O32 argument."""

import copy
import hashlib
import json
import os
import struct
import tempfile
import unittest
import zlib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from unbake.config import Held
from unbake.typemap import abi_declarations, abi_facts, evidence

try:
    from unbake.typemap import jump_tables
except ImportError:
    jump_tables = None
from unbake.typemap.mips import Analysis

FIXTURE = Path(__file__).parents[1] / "fixtures/ragewars_dispatch"
DATA = json.loads((FIXTURE / "machine.json").read_text())
NAME = "func_80295FB4_us_rev1"
VERSION = "us-rev1"
ROW = DATA["programs"][NAME][VERSION]
TABLE = DATA["table"]


def words(row):
    binary = bytes.fromhex(row["hex"])
    assert hashlib.sha256(binary).hexdigest() == row["target_sha256"]
    return list(struct.unpack(">" + str(len(binary) // 4) + "I", binary))


class Reader:
    def __init__(self, data=None):
        self.data = bytes.fromhex(TABLE["hex"]) if data is None else data
        self.reads = []
        self.spans = []

    def table_span(self, address, size):
        self.spans.append((address, size))
        if address != TABLE["address"] or size > len(self.data):
            raise Held("try", "table range is incomplete")
        return SimpleNamespace(table_entry_bias=TABLE["bias"])

    def __call__(self, address, size):
        self.reads.append((address, size))
        return self.data[:size]


def analyze(name, version, row, edges=None):
    return Analysis(
        name,
        version,
        row["address"],
        row["rom_offset"],
        words(row),
        {int(k): v for k, v in row["targets"].items()},
        {},
        **({"jump_targets": edges} if edges is not None else {}),
    ).run()


class RwDispatchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.functions = {}
        for name, versions in DATA["programs"].items():
            cls.functions[name] = {
                "aliases": [],
                "versions": {
                    version: {"address": row["address"], **analyze(name, version, row)}
                    for version, row in versions.items()
                },
            }

    def test_real_guarded_table_recovers_all_ninth_word_loads_with_one_24_byte_read(self):
        reader = Reader()
        edges = jump_tables.targets(words(ROW), ROW["address"], reader)
        self.assertEqual(reader.spans, [(0x800CA688, 24)])
        self.assertEqual(reader.reads, [(0x800CA688, 24)])
        self.assertEqual(edges, {0x8029614C: (0x80296154, 0x802963C4, 0x80296924, 0x80296620, 0x80296B84, 0x80296CA0)})
        fresh = {"address": ROW["address"], **analyze(NAME, VERSION, ROW, edges)}
        self.assertFalse(fresh["unknown"])
        self.assertIn("stack32", fresh["register_inputs"])
        self.assertEqual(
            sum(
                row.get("loaded", {}).get("origins") == [{"id": "param:" + NAME + ":stack32", "offset": 0}]
                for row in fresh["memory"]
            ),
            8,
        )
        functions = copy.deepcopy(self.functions)
        functions[NAME]["versions"][VERSION] = fresh
        abi = evidence.abi(functions)[NAME]
        self.assertEqual(abi["call_sites"], 4)
        self.assertFalse(abi["missing"])
        self.assertFalse(abi["conflicts"])
        self.assertTrue(abi["arity_known"])
        self.assertEqual(abi["registers"], ["r5", "r6", "r7", "stack16", "stack20", "stack24", "stack28", "stack32"])
        self.assertIn("r4", abi["argument_slots"])
        record = {
            "abi": abi,
            "params": [{"register": reg, "state": "unknown", "type": None} for reg in abi["registers"]],
            "return": {"state": "unknown", "type": None},
        }
        carrier = abi_declarations.prototype(NAME, record, {})
        self.assertEqual(carrier["prototype"], "void " + NAME + "(" + ", ".join(["int"] * 9) + ");")
        self.assertTrue(carrier["parameters_known"])
        self.assertEqual(sum("types.abi.unused_slot: r4" in reason for reason in carrier["reasons"]), 1)
        # Every load belongs to the same descriptor returned by the first
        # resource lookup, not an unrelated object with coincident offsets.
        offsets = {
            row["offset"]
            for row in fresh["memory"]
            if row["base"].get("origins") == [{"id": "return:" + NAME + ":us-rev1:20:r2", "offset": 0}]
            and row["width"] == 1
        }
        self.assertTrue({0, 2, 3, 5} <= offsets)

    def test_partial_or_corrupt_table_cannot_advertise_eight_arguments(self):
        for data in (b"", bytes.fromhex(TABLE["hex"])[:-4], b"\0\0\0\0" + bytes.fromhex(TABLE["hex"])[4:]):
            reader = Reader(data)
            with self.subTest(data=data.hex()):
                self.assertEqual(jump_tables.targets(words(ROW), ROW["address"], reader), {})
                self.assertLessEqual(len(reader.reads), 1)
        abi = evidence.abi(self.functions)[NAME]
        self.assertFalse(abi["arity_known"])
        self.assertTrue(any("control flow is incomplete" in reason for reason in abi["conflicts"]))
        carrier = abi_declarations.prototype(NAME, {"abi": abi, "params": [], "return": {"type": None}}, {})
        self.assertIsNone(carrier["prototype"])

    def test_refined_real_control_and_exits_replace_stale_partial_rows(self):
        binary = bytes.fromhex(ROW["hex"])
        reader = Reader()
        with tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR")) as directory:
            rom = Path(directory) / "body.bin"
            rom.write_bytes(binary)
            project = SimpleNamespace(version=lambda version: SimpleNamespace(baserom=rom))
            job = (
                NAME,
                VERSION,
                {"address": ROW["address"], "start": 0, "end": len(binary), "target_sha256": ROW["target_sha256"]},
            )
            targets = {VERSION: {int(k): v for k, v in ROW["targets"].items()}}
            with patch("unbake.decomp.rom.project_reader", return_value=reader) as build_reader:
                packed = abi_facts._refined((project, targets, {VERSION: {}}), job)
            self.assertEqual(build_reader.call_count, 1)
        record = json.loads(zlib.decompress(packed))
        stale = copy.deepcopy(self.functions[NAME])
        stale["versions"][VERSION]["calls"] = []
        stale["versions"][VERSION]["returns"] = []
        result = abi_facts.Functions({NAME: stale}, {NAME: {"versions": {VERSION: record}}})[NAME]["versions"][VERSION]
        self.assertIn("stack32", result["register_inputs"])
        self.assertFalse(result["unknown"])
        self.assertEqual(len(result["calls"]), 8)
        self.assertEqual(len(result["returns"]), 1)
        self.assertEqual(reader.reads, [(0x800CA688, 24)])

    def test_table_only_rom_change_rebuilds_complete_control_evidence(self):
        binary = bytes.fromhex(ROW["hex"])
        table = bytes.fromhex(TABLE["hex"])
        reader = Reader()
        with tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR")) as directory:
            root = Path(directory)
            rom = root / "rom.bin"
            (root / "build/map").mkdir(parents=True)
            rom.write_bytes(binary + table)
            version = SimpleNamespace(baserom=rom, baserom_sha1=hashlib.sha1(binary + table).hexdigest())
            project = SimpleNamespace(
                root=root,
                build=root / "build",
                id="bounded-rw-dispatch",
                versions=[VERSION],
                resident_mappings={},
                version=lambda name: version,
            )
            source = copy.deepcopy(self.functions[NAME])
            source["versions"][VERSION].update(start=0, end=len(binary), target_sha256=ROW["target_sha256"])
            facts = {"functions": {NAME: source}, "globals": {}, "shard_sha256": "fixed-text-shard"}
            with (
                patch("unbake.decomp.rom.project_reader", return_value=reader),
                patch("unbake.typemap.abi_facts._refined", wraps=abi_facts._refined) as work,
            ):
                first = abi_facts.refine(project, facts)
                self.assertFalse(first["functions"][NAME]["versions"][VERSION]["unknown"])
                self.assertIs(abi_facts.refine(project, first), first)
                self.assertEqual(work.call_count, 1)
                changed = b"\0\0\0\0" + table[4:]
                rom.write_bytes(binary + changed)
                version.baserom_sha1 = hashlib.sha1(binary + changed).hexdigest()
                reader.data = changed
                second = abi_facts.refine(project, first)
                self.assertEqual(work.call_count, 2)
            self.assertNotEqual(first["abi_rom_sha256"], second["abi_rom_sha256"])
            self.assertNotEqual(first["abi_supplement"]["path"], second["abi_supplement"]["path"])
            self.assertTrue(second["functions"][NAME]["versions"][VERSION]["unknown"])
        self.assertEqual(reader.reads, [(0x800CA688, 24), (0x800CA688, 24)])

    def test_de_formatter_epilogue_caller_missing_value_is_preserved(self):
        abi = evidence.abi(self.functions)["func_80414CCC_de"]
        self.assertEqual(len(abi["missing"]), 1)
        self.assertEqual(abi["missing"][0]["function"], "func_802A0AC4_de")
        self.assertEqual(abi["missing"][0]["version"], "de")
        self.assertEqual(abi["missing"][0]["register"], "r5")
        helper = self.functions["func_802BB5E0_de"]["versions"]["de"]
        self.assertEqual(helper["register_outputs"], ["r16", "r29", "r31"])
        self.assertNotIn("r4", helper["register_outputs"])
