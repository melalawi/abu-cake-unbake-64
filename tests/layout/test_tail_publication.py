"""A false split may be coalesced only with complete body and asset ownership."""

import dataclasses
import json
import struct
from unittest.mock import patch

from tests.project_fixture import ProjectCase
from unbake.layout import shape_edits, split


class TailPublicationTests(ProjectCase):
    def setUp(self):
        super().setUp()
        self.project = dataclasses.replace(self.project, names_from="us")
        for version in self.project.versions:
            row = self.project.version(version)
            row.split.write_text(
                row.split.read_text()
                .replace("[0x4C, asm, beta]", "[0x48, asm, beta]")
                .replace("[0x58, asm, gamma]", "[0x54, asm, gamma]")
            )
            base = 0x80001000 if version == "us" else 0x80202000
            row.symbols.write_text(f"alpha = 0x{base:X};\nbeta = 0x{base + 8:X};\ngamma = 0x{base + 20:X};\n")
            words = [0x24080004, 0x01041021, 0x24420001, 0x03E00008, 0, 0x24020003, 0x03E00008, 0, 0]
            row.baserom.write_bytes(bytes(0x40) + struct.pack(">9I", *words))
        self.manifest = self.project.root / "unbake-exclusions.json"
        self.manifest.write_text('{"schema":1,"functions":["beta","gamma"]}')
        self.history = self.project.work / "beta/history.jsonl"
        self.history.parent.mkdir(parents=True)
        self.history.write_text('{"historical":"retain these exact bytes"}\n')

    def run_edits(self):
        with (
            patch("unbake.pool.run", lambda host, fn, items: [fn(item) for item in items]),
            patch("unbake.config.load", return_value=self.project),
            patch("unbake.buildfiles.write", return_value=[]),
            patch("unbake.layout.merge_units._commit"),
        ):
            return shape_edits.run(self.project, self.host)

    def test_proved_continuation_removes_false_rows_and_carries_assets(self):
        before = {v: self.project.version(v).baserom.read_bytes() for v in self.project.versions}
        history = self.history.read_bytes()
        self.run_edits()
        for version in self.project.versions:
            rows = split.functions(self.project, version)
            self.assertEqual([r.name for r in rows], ["alpha", "gamma"])
            self.assertEqual((rows[0].start, rows[0].end), (0x40, 0x54))
            self.assertNotIn("beta", self.project.version(version).symbols.read_text())
            self.assertEqual(self.project.version(version).baserom.read_bytes(), before[version])
        self.assertNotIn("beta", (self.project.root / "layout.toml").read_text())
        self.assertEqual(json.loads(self.manifest.read_text())["functions"], ["gamma"])
        self.assertEqual(self.history.read_bytes(), history)

    def test_a_still_independent_version_keeps_its_name_and_exclusion(self):
        row = self.project.version("eu")
        data = bytearray(row.baserom.read_bytes())
        struct.pack_into(">I", data, 0x48, 0x24020001)
        row.baserom.write_bytes(data)
        self.run_edits()
        self.assertNotIn("beta", [r.name for r in split.functions(self.project, "us")])
        self.assertIn("beta", [r.name for r in split.functions(self.project, "eu")])
        self.assertIn("beta", (self.project.root / "layout.toml").read_text())
        self.assertIn("beta", json.loads(self.manifest.read_text())["functions"])

    def test_published_names_and_independent_calls_protect_the_entry(self):
        for reason in ("source", "call"):
            with self.subTest(reason=reason):
                self.setUp()
                if reason == "source":
                    (self.project.src / "caller.c").write_text("int caller(void){return beta();}")
                else:
                    for version in self.project.versions:
                        row = self.project.version(version)
                        base = 0x80001000 if version == "us" else 0x80202000
                        data = bytearray(row.baserom.read_bytes())
                        struct.pack_into(">I", data, 0x54, 0x0C000000 | ((base + 8) >> 2 & 0x3FFFFFF))
                        row.baserom.write_bytes(data)
                before = {v: self.project.version(v).split.read_bytes() for v in self.project.versions}
                self.run_edits()
                self.assertEqual({v: self.project.version(v).split.read_bytes() for v in self.project.versions}, before)

    def test_a_published_symbol_alias_keeps_the_cut_entry(self):
        for version in self.project.versions:
            configured = self.project.version(version)
            base = 0x80001000 if version == "us" else 0x80202000
            configured.symbols.write_text(configured.symbols.read_text() + f"entry_alias = 0x{base + 8:X};\n")
        (self.project.src / "caller.c").write_text("int caller(void) { return entry_alias(); }")
        before = {v: self.project.version(v).symbols.read_bytes() for v in self.project.versions}
        self.run_edits()
        self.assertEqual({v: self.project.version(v).symbols.read_bytes() for v in self.project.versions}, before)

    def test_three_false_rows_are_proved_and_published_as_one_body(self):
        for version in self.project.versions:
            configured = self.project.version(version)
            configured.split.write_text(
                configured.split.read_text()
                .replace("[0x54, asm, gamma]", "[0x50, asm, gamma]")
                .replace("[0x64]", "[0x5C]")
            )
            words = [0x24080004, 0x01041021, 0x24420001, 0x00454821, 0x01261021, 0x03E00008, 0]
            configured.baserom.write_bytes(bytes(0x40) + struct.pack(">7I", *words))
            base = 0x80001000 if version == "us" else 0x80202000
            configured.symbols.write_text(f"alpha = 0x{base:X};\nbeta = 0x{base + 8:X};\ngamma = 0x{base + 16:X};\n")
        self.run_edits()
        for version in self.project.versions:
            rows = split.functions(self.project, version)
            self.assertEqual([(r.name, r.start, r.end) for r in rows], [("alpha", 0x40, 0x5C)])
        self.assertEqual(json.loads(self.manifest.read_text())["functions"], [])
