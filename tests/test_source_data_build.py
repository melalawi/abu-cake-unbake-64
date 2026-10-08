"""Existing YAML source DATA rows using the actual 33,253-byte SN64 record cases."""

import gzip
import hashlib
import json
import re
from pathlib import Path
from unittest.mock import patch

from tests.project_fixture import ProjectCase
from unbake import buildfiles
from unbake.config import Held
from unbake.layout import split
from unbake.project import publication_push

FIXTURE = Path(__file__).parent / "fixtures/source_data"
RECORDS = json.loads((FIXTURE / "manifest.json").read_text())


class SourceDataBuildTests(ProjectCase):
    versions = ("us",)

    def setUp(self):
        super().setUp()
        for row in RECORDS:
            name = row["symbol"]
            raw = gzip.decompress((FIXTURE / (name + ".c.gz")).read_bytes())
            self.assertEqual(hashlib.sha256(raw).hexdigest(), row["source_sha256"])
            (self.project.src / (name + ".c")).write_bytes(raw)
            native = self.project.build_link("us") / "data" / (name + ".bin")
            native.parent.mkdir(parents=True, exist_ok=True)
            raw = gzip.decompress((FIXTURE / (name + ".bin.gz")).read_bytes())
            self.assertEqual(hashlib.sha256(raw).hexdigest(), row["binary_sha256"])
            self.assertEqual(len(raw), row["bytes"])
            native.write_bytes(raw)
        (self.project.include[-1] / "sn64_type_records.h").write_bytes((FIXTURE / "sn64_type_records.h").read_bytes())
        meta = self.project.version("us")
        self.before = (
            "name: fixture\nsegments:\n  - [0x0, header, header]\n"
            "  - name: main\n    type: code\n    start: 0xC7568\n    vram: 0x800C6968\n"
            "    subsegments:\n      - [0xC7568, data, unclaimed]\n  - [0x16E000]\n"
        )
        rows = "      - [0xC7568, data, unclaimed]\n"
        for row in RECORDS:
            rows += f'      - [0x{row["rom_start"]:X}, data, "src/{row["symbol"]}.c"]\n'
            rows += f"      - [0x{row['rom_end']:X}, data, raw_tail_{row['rom_end']:X}]\n"
        meta.split.write_text(self.before.replace("      - [0xC7568, data, unclaimed]\n", rows))
        rom = bytearray(0x16E000)
        for row in RECORDS:
            native = self.project.build_link("us") / "data" / (row["symbol"] + ".bin")
            rom[row["rom_start"] : row["rom_end"]] = native.read_bytes()
        meta.baserom.write_bytes(rom)
        self.changed = {meta.split.relative_to(self.project.root).as_posix()}
        self.calls = []

    def git(self, project, *args):
        self.calls.append(args)
        if args[:2] == ("diff", "--name-only"):
            return "\0".join(sorted(self.changed))
        if args[0] == "show":
            return self.before
        return ""

    def admit(self):
        with patch.object(publication_push, "_git", side_effect=self.git):
            return publication_push.admission(self.project, self.host, "head", "base")

    def test_actual_odd_record_rows_replace_only_claimed_raw_bytes_in_generated_pieces(self):
        units = buildfiles.units(self.project, "us")
        self.assertEqual(
            [(u.start, u.address, u.size) for u in units], [(r["rom_start"], r["vram"], r["bytes"]) for r in RECORDS]
        )
        self.assertEqual(split.functions(self.project, "us"), [])
        text = buildfiles.slices_mk(self.project, "us")
        raw = [(int(a), int(b)) for a, b in re.findall(r"^us\.S\.\w+ := (\d+) (\d+)$", text, re.M)]
        self.assertEqual(raw, [(0, 0xF8E03), (0xFDDF0, 24), (0x101000, 0x16E000 - 0x101000)])
        self.assertEqual(sum(size for _, size in raw) + sum(u.size for u in units), 0x16E000)
        for unit in units:
            self.assertIn(f"build/us/data/{unit.name}.bin", text)
            self.assertIn(f"us.D.{unit.name} := 0x{unit.address:X}", text)
        self.assertEqual(text, buildfiles.slices_mk(self.project, "us"))

    def test_actual_metadata_only_registration_checks_33253_native_bytes_and_source_hygiene(self):
        with patch.object(publication_push.split, "words", side_effect=AssertionError("DATA is byte bounded")):
            result = self.admit()
        self.assertEqual(
            result["work"],
            {
                "native_bytes_read": 33253,
                "rom_bytes_read": 33253,
                "functions_compared": 0,
                "data_extents_compared": 2,
            },
        )
        self.assertEqual(
            [(r["data"], r["bytes"]) for r in result["scopes"]], [(r["symbol"], r["bytes"]) for r in RECORDS]
        )
        self.assertEqual(len([call for call in self.calls if call[0] == "show"]), 1)
        self.before = self.project.version("us").split.read_text()
        self.assertEqual(self.admit()["scopes"], [])
        self.changed = {"src/" + RECORDS[0]["symbol"] + ".c"}
        self.assertEqual(len(self.admit()["scopes"]), 1)

    def test_actual_metadata_only_registration_refuses_missing_mismatch_truncation_and_source_rules(self):
        native = self.project.build_link("us") / "data" / (RECORDS[0]["symbol"] + ".bin")
        original = native.read_bytes()
        for payload in (original[:-1], original + b"\0", bytes([original[0] ^ 1]) + original[1:]):
            native.write_bytes(payload)
            with self.assertRaises(Held) as held:
                self.admit()
            self.assertEqual(held.exception.key, "publish.native_mismatch")
        native.unlink()
        with self.assertRaises(Held) as held:
            self.admit()
        self.assertEqual(held.exception.key, "publish.native_missing")
        self.assertIn("data/" + native.name, held.exception.reason)
        native.write_bytes(original)
        source = self.project.src / (RECORDS[0]["symbol"] + ".c")
        original_source = source.read_text()
        for forbidden in ('void bad(void) { __asm__("nop"); }', "volatile int bad_storage = 1;"):
            source.write_text(original_source + "\n" + forbidden + "\n")
            with self.assertRaises(Held) as held:
                self.admit()
            self.assertEqual(held.exception.key, "publish.push_rules")
        source.unlink()
        with self.assertRaises(Held) as held:
            buildfiles.slices_mk(self.project, "us")
        self.assertEqual(held.exception.key, "buildfiles.source")

    def test_actual_source_binding_rebounds_select_even_unchanged_sources(self):
        self.before = self.project.version("us").split.read_text().replace("0xFDDF0,", "0xFDDEF,")
        self.assertEqual([r["data"] for r in self.admit()["scopes"]], [RECORDS[0]["symbol"]])
        self.before = self.project.version("us").split.read_text().replace('data, "src/', 'rodata, "src/', 1)
        self.assertEqual([r["data"] for r in self.admit()["scopes"]], [RECORDS[0]["symbol"]])

    def test_native_data_target_reuses_compiler_cas_and_copies_before_flags_with_exact_size(self):
        text = buildfiles.makefile(self.project, self.host)
        self.assertIn("build/$1/data/%.bin: build/$1/src/%.key", text)
        self.assertIn("versions/$1/$$(NAME).data.ld | build/$1/data", text)
        recipe = text[text.index("DATA_BIN =") : text.index("SLICE =")]
        self.assertLess(recipe.index("cp build/cas/"), recipe.index("--set-section-flags"))
        self.assertIn("--section-start=.data=$(firstword $(subst :, ,$($(VER).D.$(*F))))", recipe)
        self.assertIn("--only-section=.data", recipe)
        self.assertIn("wc -c", recipe)
        self.assertNotIn("BASEROM", recipe)
        self.assertNotIn("N64LINK", recipe)
        script = buildfiles.data_link_script(self.project, "us")
        self.assertIn("*(.rdata .rdata.* .rodata", script)
        self.assertIn("INCLUDE versions/us/symbols.ld", script)
        self.assertIn("/DISCARD/ : { *(*) }", script)

    def test_same_real_source_can_own_code_and_data_but_duplicate_data_targets_fail(self):
        from types import SimpleNamespace

        row = RECORDS[0]
        code = SimpleNamespace(kind="c", path=row["symbol"], address=0x80001000, start=0x40, end=0x4C)
        with patch.object(buildfiles.split, "functions", return_value=[code]):
            text = buildfiles.slices_mk(self.project, "us")
        self.assertIn(f"us.U.{row['symbol']} := 0x80001000:0x40:0xC", text)
        self.assertIn(f"us.D.{row['symbol']} := 0x800F8203", text)
        meta = self.project.version("us")
        meta.split.write_text(meta.split.read_text().replace("data, raw_tail_FDDF0", f'data, "src/{row["symbol"]}.c"'))
        with self.assertRaises(Held) as held:
            buildfiles.units(self.project, "us")
        self.assertEqual(held.exception.key, "buildfiles.source")
