"""Committed symbol precedence and standalone build graph regressions."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.helper_fixture import extraction
from tests.project.makefile_fixture import fixture, write_rendered
from unbake.project_tools import extract


class StandaloneTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.addCleanup(patch.stopall)
        self.root = Path(self.temporary.name).resolve()

    def test_committed_symbols_override_automatic_aliases(self) -> None:
        committed = self.root / "committed.txt"
        committed.write_text("D_800C9058 = 0x800C9000;\nalias = 0x80001000;\n")
        for address in ("0x800C9058", "0X800C9058", "0x800C9000"):
            with self.subTest(address=address):
                automatic = self.root / "automatic.txt"
                automatic.write_text(
                    extract.automatic_symbols(
                        f"D_800C9058 = {address}; // alias\nalias = 0x80002000;\nkeep = 0x80003000;\n",
                        extract.symbols_from([committed]),
                    )
                )
                self.assertEqual(
                    extract.symbols_from([committed, automatic]),
                    {"D_800C9058": 0x800C9000, "alias": 0x80001000, "keep": 0x80003000},
                )
                self.assertNotIn("D_800C9058 =", automatic.read_text())
        project, _ = fixture(self.root, case=self)
        with project.version("us").symbols.open("a") as stream:
            stream.write("D_800C9058 = 0x800C9000;\n")
        splat = self.root / "tools/splat"
        splat.write_text(splat.read_text().replace("write_text('')", "write_text('D_800C9058 = 0x800C9058;\\n')"))
        write_rendered(project)
        result = extraction(project)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        build = self.root / "build/us"
        self.assertIn("PROVIDE(D_800C9058 = 0x800C9000);", (build / "committed_symbols.ld").read_text())
        self.assertIn("$(BUILD)/committed_symbols.ld", (build / ".split.mk").read_text())
        for filename in ("undefined_funcs_auto.txt", "undefined_syms_auto.txt"):
            self.assertNotIn("D_800C9058", (build / filename).read_text())

    def test_authored_c_has_no_extraction_stamp_prerequisite(self) -> None:
        staging = self.root / "staging"
        staging.mkdir()
        script = " ".join(str(staging / name) + "(.text)" for name in ("src/unit.c.o", "asm/unit.s.o"))
        _, graph = extract.inventory(script, staging, self.root / "asm", self.root / "src", "ido")
        self.assertIn(f"$(BUILD)/obj/src/unit.built: {self.root}/src/unit.c", graph)
        self.assertNotIn(f"{self.root}/src/unit.c: | $(BUILD)/.split", graph)
        self.assertIn(f"{self.root}/asm/unit.s: | $(BUILD)/.split", graph)

    def test_local_rodata_metadata_does_not_include_shared_units(self) -> None:
        for spelling in ('"local"', "local"):
            with self.subTest(spelling=spelling):
                text = (
                    "  - name: main\n    type: code\n    start: 0x1000\n    vram: 0x80071000\n"
                    f"    subsegments:\n      - [0x1350, .rodata, {spelling}]\n"
                    "      - [0x1378, rodata, shared_pool]\n"
                    "      - [0x2000, c, local]\n      - [0x2020, c, shared]\n  - [0x2040]\n"
                )
                ranges = extract.unit_ranges(text)
                self.assertEqual(ranges["local"]["rodata_address"], 0x80071350)
                self.assertNotIn("rodata_address", ranges["shared"])
                script = f"{self.root}/src/shared.c.o(.rodata);"
                for compiler in ("sn64", "ido", "gcc"):
                    rewritten, _ = extract.inventory(script, self.root, self.root / "asm", self.root / "src", compiler)
                    self.assertIn("obj/src/shared.o(.rodata)", rewritten)

    def test_plain_make_publishes_and_preserves_numbered_generation(self) -> None:
        project, _ = fixture(self.root, case=self)
        write_rendered(project)
        extract.prepare_build(self.root / "build/us")
        link = self.root / "build/us"
        self.assertTrue(link.is_symlink())
        self.assertEqual(link.readlink(), Path("us.0"))
        extract.prepare_build(link)
        self.assertEqual(link.readlink(), Path("us.0"))
        link.unlink()
        extract.prepare_build(link)
        self.assertEqual(link.readlink(), Path("us.1"))
        link.unlink()
        link.mkdir()
        (link / "receipt").write_bytes(b"preserved")
        extract.prepare_build(link)
        self.assertEqual(link.readlink(), Path("us.2"))
        self.assertEqual((link / "receipt").read_bytes(), b"preserved")
