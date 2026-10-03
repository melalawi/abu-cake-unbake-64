"""Placement evidence using small MIPS instruction and split fixtures."""

import struct
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from unbake.decomp.needs import PlacementNeed
from unbake.layout import split, split_edits, xver, xver_edits
from unbake.project.config import Held
from unbake.project_tools.extract import alignment_rows, render_alignment

BODY = bytes.fromhex("27bdffe0 afbf001c 0c109230 00000000 3c04800c c4848884 8fbf001c 03e00008 27bd0020")


class Project:
    def __init__(self, root: Any) -> None:
        self.root = root
        self.names_from = "us"
        self.versions = ("us", "eu-x")
        self.maps = {}
        for version in self.versions:
            directory = root / version
            directory.mkdir()
            self.maps[version] = SimpleNamespace(
                split=directory / "split.yaml", symbols=directory / "symbols.txt", baserom=directory / "rom.z64"
            )

    def version(self, version: Any) -> Any:
        return self.maps[version]

    def layout(self, version: Any, rows: Any, symbols: Any = "", align: Any = 4) -> None:
        item = self.version(version)
        text = (
            "segments:\n  - name: main\n    type: code\n    start: 0x40\n    vram: 0x80200000\n"
            f"    subalign: {align}\n    subsegments:\n"
        )
        text += "".join(f"      - [0x{offset:X}, {kind}, {name}]\n" for offset, kind, name in rows)
        text += "  - [0x180]\n"
        item.split.write_text(text)
        item.symbols.write_text(symbols)

    def image(self, version: Any, chunks: Any, order: Any = 1) -> None:
        data = bytearray(0x180)
        data[:4] = bytes.fromhex("80371240")
        for offset, content in chunks:
            data[offset : offset + len(content)] = content
        if order != 1:
            data = b"".join(data[offset : offset + order][::-1] for offset in range(0, len(data), order))
        self.version(version).baserom.write_bytes(data)


class PlacementTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.project = Project(Path(temporary.name).resolve())

    def prepare(
        self, name: Any = "entry", target: Any = "other", merged: Any = False, order: Any = 1, body: Any = BODY
    ) -> Any:
        size = len(body)
        self.project.layout("us", [(0x40, "asm", name), (0x40 + size, "data", "data")])
        self.project.image("us", [(0x40, body)])
        start = 0x60 if merged else 0x40
        self.project.layout("eu-x", [(0x40, "asm", target), (start + size, "data", "data")])
        other = bytearray(body)
        if len(other) == len(BODY):
            struct.pack_into(">I", other, 8, 0x0C113CCC)
            struct.pack_into(">I", other, 16, 0x3C04800D)
            struct.pack_into(">I", other, 20, 0xC4848000)
        self.project.image("eu-x", [(start, other)], order)
        return start

    def test_missing_renamed_and_merged_placements(self) -> None:
        for name, merged, expected in [
            ("func_8026D83C", True, "cut"),
            ("func_80243A80", True, "cut"),
            ("func_80286A78", False, "rename"),
            ("func_8043E054", False, "rename"),
        ]:
            with self.subTest(name=name):
                start = self.prepare(name, merged=merged)
                if name == "func_80243A80":
                    # A named entry may begin at an instruction used as a delay slot.
                    item = self.project.version("eu-x")
                    item.symbols.write_text(f"{name} = 0x80200020;\n")
                before = {
                    path: path.read_bytes()
                    for item in self.project.maps.values()
                    for path in (item.split, item.symbols)
                }
                pending = xver.needs(self.project, name)
                self.assertIn(expected, [need.action for need in pending])
                self.assertEqual(xver.locate(self.project, name)["eu-x"].start, start)
                edits = xver_edits.resolve(pending, self.project, object())
                self.assertTrue(any(f"asm, {name}" in edit.after for edit in edits))
                for path, content in before.items():
                    self.assertEqual(path.read_bytes(), content)
                for edit in edits:
                    edit.path.write_text(edit.after)
                self.assertFalse(xver.needs(self.project, name))

    def test_rename_after_symbol_resolution_and_worklist(self) -> None:
        self.prepare("entry", "twin")
        self.project.version("eu-x").symbols.write_text(
            "twin = 0x80200000;\nentry = 0x80200000; // type:func size:0x24\n"
        )
        item = self.project.version("us")
        item.split.write_text(item.split.read_text().replace("asm, entry", "c, entry"))
        pending = xver.twins(self.project)
        self.assertEqual([need.action for need in pending], ["place", "rename"])
        edits = xver_edits.resolve(pending, self.project, object())
        symbols = next(edit for edit in edits if edit.path == self.project.version("eu-x").symbols)
        self.assertNotIn("twin =", symbols.after)
        self.assertIn("entry = 0x80200000; // type:func size:0x24", symbols.after)

    def test_byte_orders_preserve_registers_and_branch_words(self) -> None:
        for order in (1, 2, 4):
            with self.subTest(order=order):
                self.prepare(order=order)
                self.assertIsNotNone(xver.locate(self.project, "entry")["eu-x"])
        for index, changed in [(0, 0x27BCFFE0), (16, 0x3C05800D), (24, 0x8FBE001C)]:
            with self.subTest(index=index):
                self.prepare()
                data = bytearray(self.project.version("eu-x").baserom.read_bytes())
                struct.pack_into(">I", data, 0x40 + index, changed)
                self.project.version("eu-x").baserom.write_bytes(data)
                self.assertIsNone(xver.locate(self.project, "entry")["eu-x"])

    def test_alignment_boundaries_and_linker_fragment(self) -> None:
        for alignment in (2, 4, 8, 16):
            with self.subTest(alignment=alignment):
                self.prepare()
                path = self.project.version("us").split
                before = path.read_text()
                edit = split_edits.align(self.project, "us", "entry", alignment)[0]
                self.assertEqual(path.read_text(), before)
                stripped, values = alignment_rows(edit.after)
                self.assertEqual(stripped, before)
                self.assertEqual(values, {"entry": alignment})
                script = render_alignment("    obj/src/entry.o(.text)\n", values)
                self.assertIn(f". = ALIGN({alignment});", script)
                path.write_text(edit.after)
                self.assertEqual(split_edits.align(self.project, "us", "entry", alignment), [])
                self.assertEqual(split.functions(self.project, "us")[0].name, "entry")
        self.prepare(body=bytes.fromhex("03e00008 00000000 00000000 00000000"))
        self.project.layout("us", [(0x40, "asm", "entry"), (0x50, "data", "data")], align=16)
        self.assertEqual(xver.locate(self.project, "entry")["us"].align, 16)

    def test_refusals_name_the_missing_or_conflicting_value(self) -> None:
        self.prepare()
        for value in (None, False, 0, 3, -1):
            with self.subTest(value=value), self.assertRaisesRegex(Held, "align"):
                split_edits.align(self.project, "us", "entry", value)
        for action, value, message in [("unknown", 4, "action"), ("place", None, "value")]:
            with self.subTest(action=action), self.assertRaisesRegex(Held, message):
                xver_edits.resolve(
                    [PlacementNeed("us", "entry", 0x40, 0x64, action, value, "")], self.project, object()
                )
        with self.assertRaisesRegex(Held, "missing"):
            xver.locate(self.project, "missing")
        del self.project.names_from
        with self.assertRaisesRegex(Held, "names_from"):
            xver.locate(self.project, "entry")

    def test_ambiguity_empty_and_truncated_words(self) -> None:
        self.prepare()
        self.project.layout("eu-x", [(0x40, "asm", "merged"), (0xA4, "data", "data")])
        self.project.image("eu-x", [(0x40, BODY), (0x80, BODY)])
        with self.assertRaisesRegex(Held, "ambiguous"):
            xver.locate(self.project, "entry")
        with self.assertRaisesRegex(Held, "missing symbol entry address.*0x80200000.*ROM 0x40.*0x80200040"):
            xver.locate(self.project, "entry")
        for data, start, end in [(b"", 0, 0), (b"12345", 0, 5), (b"1234", 0, 8)]:
            with self.subTest(data=data), self.assertRaisesRegex(Held, "start/end"):
                xver.body(data, start, end, "entry")
        for text in ("      - [0x40, c, entry, {align: 3}]\n", "      - [0x40, data, data, {align: 4}]\n"):
            with self.subTest(text=text), self.assertRaisesRegex(ValueError, "align"):
                alignment_rows(text)
        with self.assertRaisesRegex(ValueError, "entry"):
            render_alignment("", {"entry": 16})

    def test_named_jump_operand_disambiguates_relocation_twins(self) -> None:
        body = bytes.fromhex("0c080020 00000000 03e00008 00000000")
        self.project.layout("us", [(0x40, "asm", "entry"), (0x50, "data", "pool")], "callee = 0x80200080;\n")
        self.project.image("us", [(0x40, body)])
        self.project.layout("eu-x", [(0x40, "asm", "merged"), (0x90, "data", "pool")], "callee = 0x802000A0;\n")
        other = bytes.fromhex("0c080028 00000000 03e00008 00000000")
        self.project.image("eu-x", [(0x40, body), (0x80, other)])
        span = xver.locate(self.project, "entry")["eu-x"]
        self.assertIsNotNone(span)
        self.assertEqual(span.start, 0x80)

    def test_named_caller_disambiguates_an_identical_empty_function(self) -> None:
        empty = bytes.fromhex("03e00008 00000000")
        caller = bytes.fromhex("0c080000 00000000 03e00008 00000000")
        self.project.layout(
            "us", [(0x40, "asm", "entry"), (0x48, "data", "pool"), (0x80, "asm", "caller"), (0x90, "data", "tail")]
        )
        self.project.image("us", [(0x40, empty), (0x80, caller)])
        self.project.layout(
            "eu-x", [(0x40, "asm", "merged"), (0x70, "data", "pool"), (0x80, "asm", "caller"), (0x90, "data", "tail")]
        )
        target_caller = bytes.fromhex("0c080008 00000000 03e00008 00000000")
        self.project.image("eu-x", [(0x40, empty), (0x60, empty), (0x80, target_caller)])
        self.assertEqual(xver.locate(self.project, "entry")["eu-x"].start, 0x60)

    def test_named_memory_operand_disambiguates_hi_lo_twins(self) -> None:
        self.prepare()
        self.project.version("us").symbols.write_text("data = 0x800B8884;\n")
        self.project.layout("eu-x", [(0x40, "asm", "merged"), (0xA4, "data", "pool")], "data = 0x800C8000;\n")
        changed = bytearray(BODY)
        struct.pack_into(">I", changed, 16, 0x3C04800D)
        struct.pack_into(">I", changed, 20, 0xC4848000)
        self.project.image("eu-x", [(0x40, BODY), (0x80, changed)])
        self.assertEqual(xver.locate(self.project, "entry")["eu-x"].start, 0x80)

    def test_selected_version_proof_does_not_require_other_versions(self) -> None:
        self.prepare()
        root = self.project.root / "de"
        root.mkdir()
        self.project.maps["de"] = SimpleNamespace(
            split=root / "split.yaml", symbols=root / "symbols.txt", baserom=root / "rom.z64"
        )
        self.project.versions = (*self.project.versions, "de")
        self.project.layout("de", [(0x40, "asm", "merged"), (0xA4, "data", "pool")])
        self.project.image("de", [(0x40, BODY), (0x80, BODY)])
        with self.assertRaisesRegex(Held, "VERSION de.*ambiguous"):
            xver.locate(self.project, "entry")
        spans = xver.locate(self.project, "entry", versions=["eu-x"])
        self.assertEqual(set(spans), {"us", "eu-x"})
        self.assertEqual(spans["eu-x"].start, 0x40)

    def test_indexed_high_half_flows_through_either_source_and_another_destination(self) -> None:
        for arithmetic in (0x00220821, 0x00410821, 0x00221821, 0x00411821):
            with self.subTest(arithmetic=arithmetic):
                base = (arithmetic >> 11 & 31) << 21
                code = [0x3C018001, arithmetic, 0x8C020100 | base, 0x03E00008, 0]
                self.assertEqual(xver._masks(code), [0xFFFF, 0, 0xFFFF, 0, 0])
                self.prepare(body=struct.pack(">5I", *code))
                code[0] = 0x3C018002
                code[2] += 0x100
                self.project.image("eu-x", [(0x40, struct.pack(">5I", *code))])
                self.assertIsNotNone(xver.locate(self.project, "entry")["eu-x"])
                code[1] ^= 0x00200000
                self.project.image("eu-x", [(0x40, struct.pack(">5I", *code))])
                self.assertIsNone(xver.locate(self.project, "entry")["eu-x"])

    def test_masks_preserve_nonaddress_immediates_and_stop_at_overwrites(self) -> None:
        for code in (
            [0x3C013F80, 0x34211234],  # Float/integer constant.
            [0x3C010002, 0x24210001],  # Ordinary integer materialization.
            [0x3C018001, 0x24010004, 0x8C220100],  # Base overwritten by a constant.
            [0x3C018001, 0x8C810000, 0x8C220100],  # Base overwritten by a load.
            [0x3C018001, 0x00430821, 0x8C220100],  # Unrelated address arithmetic.
            [0x3C018001, 0x3C028002, 0x00220821, 0x8C220100],  # Two address bases.
            [0x3C018001, 0x48210000, 0x8C220100],  # Coprocessor overwrites GPR.
        ):
            with self.subTest(code=code):
                self.assertEqual(xver._masks(code), [0] * len(code))
        code = [0x3C018001, 0x24210100, 0x24210004, 0x8C220000]
        self.assertEqual(xver._masks(code), [0xFFFF, 0xFFFF, 0, 0])
        # Materializing into another register does not destroy the original hi.
        code = [0x3C018001, 0x24230100, 0x8C220104]
        self.assertEqual(xver._masks(code), [0xFFFF, 0xFFFF, 0xFFFF])

    def test_index_arithmetic_immediates_are_not_low_half_relocations(self) -> None:
        code = [0x3C018001, 0x00220821, 0x24210004, 0x8C220100]
        self.assertEqual(xver._masks(code), [0, 0, 0, 0])
        code = [0x3C018001, 0x00220821, 0x34210004, 0x8C220100]
        self.assertEqual(xver._masks(code), [0, 0, 0, 0])

    def test_location_requires_address_provenance_in_the_target_too(self) -> None:
        code = [0x3C018001, 0x8C220000, 0x03E00008, 0]
        self.prepare(body=struct.pack(">4I", *code))
        code[0] = 0x3C010000
        self.project.image("eu-x", [(0x40, struct.pack(">4I", *code))])
        self.assertIsNone(xver.locate(self.project, "entry")["eu-x"])
