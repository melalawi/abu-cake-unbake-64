"""Data renames preserve proved cross-VERSION addresses."""

import struct
import tempfile
import unittest
from pathlib import Path
from typing import cast

from tests.layout.test_split import ProjectFixture
from unbake.decomp.symbols_edits import data_symbol
from unbake.layout import data_symbols
from unbake.project.config import Held, Policy, Project


class DataProjectFixture(ProjectFixture):
    names_from: str = "us"


class DataRenameTests(unittest.TestCase):
    def test_missing_rows_follow_aligned_code_and_validated_command(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = DataProjectFixture(Path(temporary).resolve())
            project = cast(Project, fixture)
            policy = cast(Policy, fixture.policy)
            for version in project.versions:
                fixture.layout(version, [(0x10, "asm", "alpha"), (0x30, "asm", "beta"), (0x40, "data", "pool")])
                layout = project.version(version).split
                layout.write_text(layout.read_text().replace("    subsegments:", "    subalign: 4\n    subsegments:"))
                code = ([0] if version == "eu" else []) + [
                    0x3C038000,
                    0x8C642034 if version == "us" else 0x8C643034,
                    0x03E00008,
                    0,
                ]
                image = bytearray(128)
                image[:4] = bytes.fromhex("80371240")
                image[0x10 : 0x10 + len(code) * 4] = struct.pack(f">{len(code)}I", *code)
                project.version(version).baserom.write_bytes(image)
            source = project.version("us").symbols
            source.write_text(source.read_text() + "value = 0x80002034;\n")
            self.assertEqual(data_symbols.addresses(project, "value"), {"us": 0x80002034, "eu": 0x80003034})
            edits = data_symbols.correspondence(project, policy, "value")
            self.assertEqual(len(edits), 1)
            self.assertIn("value = 0x80003034;", edits[0].after)
            edits[0].path.write_text(edits[0].after)
            self.assertEqual(data_symbols.correspondence(project, policy, "value"), [])
            alias = data_symbol(project, policy, "eu", "alias", 0x80003034, None)
            self.assertIn("alias = 0x80003034; // absolute:True", alias[0].after)
            renamed = data_symbol(project, policy, "eu", "shared", 0x80003034, "value")
            self.assertIn("shared = 0x80003034;", renamed[0].after)
            for name, address, old in (
                ("value", 0x80003038, None),
                ("bad", 0x80001000, None),
                ("shared", 0x80003038, "value"),
                ("bad", -1, None),
            ):
                with self.subTest(name=name, address=address, old=old), self.assertRaises(Held):
                    data_symbol(project, policy, "eu", name, address, old)
            target = project.version("eu")
            target.symbols.write_text(target.symbols.read_text().replace("value = 0x80003034;\n", ""))
            target.baserom.write_bytes(bytes(128))
            with self.assertRaises(Held):
                data_symbols.addresses(project, "value")

    def test_implicit_address_names_follow_their_naming_version_and_refuse_ambiguity(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = DataProjectFixture(Path(temporary).resolve(), ("us", "us-rev1", "eu", "eu-x", "de"))
            project = cast(Project, fixture)
            expected = {}
            for index, version in enumerate(project.versions):
                fixture.layout(version, [(0x10, "asm", "alpha"), (0x30, "asm", "beta"), (0x40, "data", "pool")])
                layout = project.version(version).split
                layout.write_text(layout.read_text().replace("    subsegments:", "    subalign: 4\n    subsegments:"))
                address = 0x80002034 + index * 0x1000
                expected[version] = address
                code = [0x3C038000, 0x8C640000 | (address & 0xFFFF), 0x03E00008, 0]
                image = bytearray(128)
                image[:4] = bytes.fromhex("80371240")
                for offset in (0x10, 0x30):
                    image[offset : offset + 16] = struct.pack(">4I", *code)
                project.version(version).baserom.write_bytes(image)
            before = {v: project.version(v).symbols.read_bytes() for v in project.versions}
            for origin in ("us", "eu"):
                fixture.names_from = origin
                name = f"D_{expected[origin]:08X}"
                self.assertEqual(data_symbols.addresses(project, name), expected)
                edits = data_symbols.correspondence(project, cast(Policy, fixture.policy), name)
                self.assertEqual(len(edits), 5)
                self.assertEqual({v: project.version(v).symbols.read_bytes() for v in project.versions}, before)
            target = project.version("de")
            image = bytearray(target.baserom.read_bytes())
            image[0x34:0x38] = struct.pack(">I", 0x8C646038)
            target.baserom.write_bytes(image)
            with self.assertRaisesRegex(Held, "no unambiguous aligned reference in VERSION de"):
                data_symbols.addresses(project, name)

    def test_indexed_relocation_in_the_locator_preserves_aligned_data_references(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = DataProjectFixture(Path(temporary).resolve())
            project = cast(Project, fixture)
            for version, array_low, value_low in (("us", 0x100, 0x300), ("eu", 0x200, 0x400)):
                fixture.layout(version, [(0x10, "asm", "alpha" if version == "us" else "peer"), (0x2C, "data", "pool")])
                code = [
                    0x3C018000,
                    0x00220821,
                    0x8C220000 | array_low,
                    0x3C048000,
                    0x8C830000 | value_low,
                    0x03E00008,
                    0,
                ]
                image = bytearray(128)
                image[:4] = bytes.fromhex("80371240")
                image[0x10:0x2C] = struct.pack(">7I", *code)
                project.version(version).baserom.write_bytes(image)
            source = project.version("us").symbols
            source.write_text(source.read_text() + "value = 0x80000300;\n")
            self.assertEqual(data_symbols.addresses(project, "value"), {"us": 0x80000300, "eu": 0x80000400})
