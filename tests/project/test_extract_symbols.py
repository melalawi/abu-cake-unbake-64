"""Symbol inventory includes labels inside compiled C intervals."""

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.project.makefile_fixture import fixture, helper, write_rendered
from tests.support import tool


class DiscoveredSymbols(unittest.TestCase):
    def test_canonical_unit_alias_links_and_real_definition_wins(self) -> None:
        self.addCleanup(patch.stopall)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project, _ = fixture(root)
            write_rendered(project)
            symbols = root / "versions/us/symbol_addrs.txt"
            symbols.write_text("")
            splat = root / "tools/splat"
            splat.write_text(splat.read_text() + "dump.write_text('name,vram_start\\nfirst_auto,80000000\\n')\n")
            command = ["make", "VERSION=us", "extract"]
            result = subprocess.run(command, cwd=root, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            definitions = root / "build/us/committed_symbols.ld"
            self.assertIn("PROVIDE(first = 0x80000000);", definitions.read_text())
            source = root / "caller.s"
            source.write_text(".text\n.globl first_auto, middle\nfirst_auto:\n.word first\nmiddle:\n.word middle\n")
            obj, elf, image = (root / name for name in ("caller.o", "caller.elf", "caller.bin"))
            subprocess.run(
                [tool("mips-linux-gnu-as"), "-EB", "--no-pad-sections", "-o", str(obj), str(source)], check=True
            )
            script = root / "link.ld"
            script.write_text("SECTIONS { .text 0x90000000 : { *(.text) } }")
            subprocess.run(
                [tool("mips-linux-gnu-ld"), "-T", str(script), "-T", str(definitions), "-o", str(elf), str(obj)],
                check=True,
            )
            subprocess.run([tool("mips-linux-gnu-objcopy"), "-O", "binary", str(elf), str(image)], check=True)
            self.assertEqual(image.read_bytes()[:8], bytes.fromhex("8000000090000004"))
            symbols.write_text("first = 0x80000004;\n")
            result = subprocess.run(command, cwd=root, capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("split and symbols disagree for first", result.stderr)

    def test_c_labels_and_conflicts(self) -> None:
        extract = helper("extract")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "symbols.csv"
            path.write_text("vram_start,name,subsegment_type\n80201004,inner,c\n800C2000,constant,c\n")
            self.assertEqual(
                extract.discovered_symbols(path, {"entry": 0x80201000}),
                {"entry": 0x80201000, "inner": 0x80201004, "constant": 0x800C2000},
            )
            with self.assertRaisesRegex(ValueError, "conflicting discovered symbol"):
                extract.discovered_symbols(path, {"inner": 0x80201008})

    def test_extraction_suppresses_recovered_output_and_preserves_failure(self) -> None:
        self.addCleanup(patch.stopall)
        for fails in (False, True):
            with self.subTest(fails=fails), tempfile.TemporaryDirectory() as directory:
                patch.stopall()
                root = Path(directory)
                project, _ = fixture(root)
                write_rendered(project)
                splat = root / "tools/splat"
                splat.write_text(
                    splat.read_text()
                    + "\nimport sys\nprint('recovered symbol diagnostic')\n"
                    + "print('extractor diagnostic', file=sys.stderr)\n"
                    + ("sys.exit(7)\n" if fails else "")
                )
                result = subprocess.run(["make", "VERSION=us", "extract"], cwd=root, capture_output=True, text=True)
                output = result.stdout + result.stderr
                self.assertEqual(result.returncode != 0, fails, output)
                self.assertEqual("extractor diagnostic" in output, fails)
                self.assertEqual("recovered symbol diagnostic" in output, fails)
                self.assertEqual("HELD(extract)" in output, fails)
