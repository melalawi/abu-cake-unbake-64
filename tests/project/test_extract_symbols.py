"""Symbol inventory includes labels inside compiled C intervals."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.helper_fixture import extraction
from tests.project.makefile_fixture import fixture, helper, write_rendered


class DiscoveredSymbols(unittest.TestCase):
    def test_canonical_unit_alias_links_and_real_definition_wins(self) -> None:
        self.addCleanup(patch.stopall)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            project, _ = fixture(root, case=self)
            write_rendered(project)
            symbols = root / "versions/us/symbol_addrs.txt"
            symbols.write_text("")
            splat = root / "tools/splat"
            splat.write_text(splat.read_text() + "dump.write_text('name,vram_start\\nfirst_auto,80000000\\n')\n")
            result = extraction(project)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            definitions = root / "build/us/committed_symbols.ld"
            self.assertIn("PROVIDE(first = 0x80000000);", definitions.read_text())
            symbols.write_text("first = 0x80000004;\n")
            result = extraction(project)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("split and symbols disagree for first", result.stderr)

    def test_c_labels_and_conflicts(self) -> None:
        extract = helper("extract")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory).resolve() / "symbols.csv"
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
                root = Path(directory).resolve()
                project, _ = fixture(root, case=self)
                write_rendered(project)
                splat = root / "tools/splat"
                splat.write_text(
                    splat.read_text()
                    + "\nimport sys\nprint('recovered symbol diagnostic')\n"
                    + "print('extractor diagnostic', file=sys.stderr)\n"
                    + ("sys.exit(7)\n" if fails else "")
                )
                result = extraction(project)
                output = result.stdout + result.stderr
                self.assertEqual(result.returncode != 0, fails, output)
                self.assertEqual("extractor diagnostic" in output, fails)
                self.assertEqual("recovered symbol diagnostic" in output, fails)
                self.assertEqual("HELD(extract)" in output, fails)
