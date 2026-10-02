"""Relocation evidence, component contradictions and version-local publication."""

import struct
import unittest
from dataclasses import replace

from unbake.layout.split import Function
from unbake.layout.symbol_identity import DataSymbol, data_identity
from unbake.layout.symbol_replan import data_symbols_text


class DataIdentityTests(unittest.TestCase):
    def fixture(self, addresses):
        images, functions, tables = {}, {}, {}
        for version, values in addresses.items():
            code = []
            for address in values:
                code.extend([0x3C020000 | ((address + 0x8000) >> 16), 0x8C420000 | (address & 65535)])
            code.extend([0x03E00008, 0])
            images[version] = struct.pack(f">{len(code)}I", *code)
            functions[version] = [Function(version, "shared", 0, len(code) * 4, 0x80400000, "shared", "asm", ())]
            tables[version] = {f"D_{address:08X}": DataSymbol(address) for address in values}
        return images, functions, tables

    def test_declared_order_names_subset_and_bindings_preserve_addresses(self):
        args = self.fixture({"eu": [0x80021000], "de": [0x80011000]})
        result = data_identity(*args)
        self.assertEqual(result["unified"], 1)
        self.assertEqual(result["objects"][0]["name"], "D_80021000")
        self.assertEqual(result["renames"]["de"], {"D_80011000": "D_80021000"})
        output = data_symbols_text("D_80011000 = 0x80011000; // size:4\n", "de", {}, result)
        self.assertEqual(output, "D_80021000 = 0x80011000; // size:4\n")
        self.assertEqual(args[0]["de"], self.fixture({"de": [0x80011000]})[0]["de"])

    def test_repeated_target_contradiction_refuses_entire_component(self):
        result = data_identity(*self.fixture({"de": [0x80011000, 0x80011000], "eu": [0x80021000, 0x80022000]}))
        self.assertEqual(result["unified"], 0)
        self.assertEqual(result["contradictions_by_reason"], {"data.symbol_multiple_objects": 1})
        self.assertFalse(any(result["renames"].values()))

    def test_size_kind_conflicts_are_named_and_unknown_metadata_does_not_conflict(self):
        images, functions, tables = self.fixture({"de": [0x80011000], "eu": [0x80021000]})
        tables["de"]["D_80011000"] = DataSymbol(0x80011000, 4, "u32")
        tables["eu"]["D_80021000"] = DataSymbol(0x80021000, 8, "f64")
        result = data_identity(images, functions, tables)
        self.assertEqual(set(result["contradictions_by_reason"]), {"data.size_conflict", "data.kind_conflict"})
        tables["eu"]["D_80021000"] = DataSymbol(0x80021000)
        self.assertEqual(data_identity(images, functions, tables)["unified"], 1)
        declarations = {"D_80011000": "int", "D_80021000": "int[]"}
        self.assertEqual(
            data_identity(images, functions, tables, declarations=declarations)["contradictions_by_reason"],
            {"data.kind_conflict": 1},
        )

    def test_different_addends_and_unshared_items_do_not_pair(self):
        images, functions, tables = self.fixture({"de": [0x80011000], "eu": [0x80021004]})
        tables["eu"] = {"D_80021000": DataSymbol(0x80021000, 8)}
        self.assertEqual(data_identity(images, functions, tables)["unified"], 0)
        functions["eu"] = [replace(functions["eu"][0], name="other")]
        self.assertEqual(data_identity(images, functions, tables)["unified"], 0)

    def test_assertions_share_the_component_conflict_checks(self):
        images, functions, tables = self.fixture({"de": [0x80011000], "eu": [0x80021000]})
        assertion = dict(
            name="object",
            placements=[
                dict(version=v, symbol=n, address=s.address) for v, rows in tables.items() for n, s in rows.items()
            ],
            evidence="reviewed data layout",
        )
        result = data_identity(images, functions, tables, assertions=[assertion])
        self.assertEqual(result["objects"][0]["name"], "object")
        self.assertIn("object = 0x80011000;", data_symbols_text("", "de", {}, result))
