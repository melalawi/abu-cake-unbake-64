"""Placement evidence using small MIPS instruction and split fixtures."""

import struct
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from unbake.decomp.needs import PlacementNeed, SymbolNeed
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
        self.project = Project(Path(temporary.name))

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
                trial = SimpleNamespace(function=name, needs=[])
                pending = xver.needs(self.project, name, trial)
                self.assertIn(expected, [need.action for need in pending])
                self.assertEqual(xver.locate(self.project, name)["eu-x"].start, start)
                edits = xver_edits.resolve(pending, self.project, object())
                self.assertTrue(any(f"asm, {name}" in edit.after for edit in edits))
                for path, content in before.items():
                    self.assertEqual(path.read_bytes(), content)
                for edit in edits:
                    edit.path.write_text(edit.after)
                self.assertFalse(xver.needs(self.project, name, trial))

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

    def test_callee_twins_need_the_requested_address(self) -> None:
        for name in ("func_8044EBB0", "func_8044BD30", "func_802192C0"):
            with self.subTest(name=name):
                self.prepare(name, merged=True)
                requested = SymbolNeed("eu-x", name, 0x80200020, 0, ".text", "func", len(BODY), "jump")
                trial = SimpleNamespace(function=name, needs=[requested])
                self.assertIn("cut", [need.action for need in xver.needs(self.project, name, trial)])
                trial.needs = [SymbolNeed("eu-x", name, 0x80200024, 0, ".text", "func", len(BODY), "jump")]
                with self.assertRaisesRegex(Held, name):
                    xver.needs(self.project, name, trial)

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
        for trial, message in [
            (None, "trial"),
            (SimpleNamespace(function="other", needs=[]), "trial.function"),
            (SimpleNamespace(function="entry"), "trial.needs"),
        ]:
            with self.subTest(message=message), self.assertRaisesRegex(Held, message):
                xver.needs(self.project, "entry", trial)
        with self.assertRaisesRegex(Held, "missing"):
            xver.locate(self.project, "missing")
        del self.project.names_from
        with self.assertRaisesRegex(Held, "names_from"):
            xver.locate(self.project, "entry")

    def test_object_call_evidence_and_named_refusals(self) -> None:
        self.prepare()
        address = 0x80200000
        jump = 0x0C000000 | ((address >> 2) & 0x03FFFFFF)
        from tests.support import tool
        from unbake.project_tools.elf import Object

        source = self.project.root / "call.s"
        source.write_text(".set noreorder\n.text\njal entry\nnop\n")
        path = source.with_suffix(".o")
        subprocess.run(
            [tool("mips-linux-gnu-as"), "-EB", "-o", str(path), str(source)], check=True, capture_output=True
        )
        original = path.read_bytes()
        artifact = {
            "unit": SimpleNamespace(path=path),
            "target_words": [jump],
            "span": SimpleNamespace(address=0x80201000),
        }
        context = SimpleNamespace(
            project=self.project, trial=SimpleNamespace(function="entry"), artifacts={"us": artifact}
        )
        result = xver.derive(context)
        self.assertTrue(any(isinstance(need, SymbolNeed) and need.address == address for need in result))
        for field in ("unit", "target_words", "span"):
            saved = artifact.pop(field)
            with self.subTest(field=field), self.assertRaisesRegex(Held, field):
                xver.derive(context)
            artifact[field] = saved
        obj = Object(path)
        text_offset = obj.sections[obj.section(".text")][4]
        relocation_offset = obj.sections[obj.section(".rel.text")][4]
        # A draft shifted against its target proves no callee address; the comparison reports the shift.
        artifact["target_words"] = [0]
        self.assertFalse(any(isinstance(need, SymbolNeed) for need in xver.derive(context)))
        artifact["target_words"] = [jump]
        for offset, target, content, message in [
            (1, jump, bytes.fromhex("0c000000"), "offset"),
            (0, jump, bytes.fromhex("0c000001"), "addend"),
        ]:
            data = bytearray(original)
            struct.pack_into(">I", data, relocation_offset, offset)
            data[text_offset : text_offset + 4] = content
            path.write_bytes(data)
            artifact["target_words"] = [target]
            with self.subTest(message=message), self.assertRaisesRegex(Held, message):
                xver.derive(context)
        self.prepare()
        with self.assertRaisesRegex(Held, "policy"):
            xver_edits.resolve([], self.project, None)
        path = self.project.version("us").split
        edit = split_edits.align(self.project, "us", "entry", 8)[0]
        path.write_text(edit.after)
        with self.assertRaisesRegex(Held, "conflicts"):
            split_edits.align(self.project, "us", "entry", 16)
        with self.assertRaisesRegex(Held, "missing"):
            split_edits.align(self.project, "us", "missing", 16)
        self.prepare()
        self.project.version("eu-x").baserom.write_bytes(b"")
        with self.assertRaisesRegex(Held, "baserom"):
            xver.locate(self.project, "entry")

    def test_ambiguity_empty_and_truncated_words(self) -> None:
        self.prepare()
        self.project.layout("eu-x", [(0x40, "asm", "merged"), (0xA4, "data", "data")])
        self.project.image("eu-x", [(0x40, BODY), (0x80, BODY)])
        with self.assertRaisesRegex(Held, "ambiguous"):
            xver.locate(self.project, "entry")
        for data, start, end in [(b"", 0, 0), (b"12345", 0, 5), (b"1234", 0, 8)]:
            with self.subTest(data=data), self.assertRaisesRegex(Held, "start/end"):
                xver.body(data, start, end, "entry")
        for text in ("      - [0x40, c, entry, {align: 3}]\n", "      - [0x40, data, data, {align: 4}]\n"):
            with self.subTest(text=text), self.assertRaisesRegex(ValueError, "align"):
                alignment_rows(text)
        with self.assertRaisesRegex(ValueError, "entry"):
            render_alignment("", {"entry": 16})
