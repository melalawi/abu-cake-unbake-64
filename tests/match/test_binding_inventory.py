"""Retained linker bindings and relocation inventories agree after advancement."""

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from unbake.match import incremental


class BindingInventoryTests(unittest.TestCase):
    def test_reconciles_inherited_bindings_and_new_aliases_idempotently(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            symbols = root / "symbols.txt"
            symbols.write_text("new = 0x80002000;\n// unbake linker alias: alias = 0x80001000;\n")
            project = SimpleNamespace(version=lambda version: SimpleNamespace(symbols=symbols))
            (root / "committed_symbols.ld").write_text("PROVIDE(old = 0x80001000);\n")
            (root / "symbol-addresses.txt").write_text("entry 0x80000000 unit\n")
            for name in ("undefined_funcs_auto.txt", "undefined_syms_auto.txt"):
                (root / name).write_text("new = 0x80002000;\nuntouched = 0x80003000;\n")
            with patch("unbake.match.relink.retarget_rows", return_value=True):
                self.assertTrue(incremental.advance(project, "us", root, "before", "after"))
                first = (root / "symbol-addresses.txt").read_text()
                for name, address in (("old", "80001000"), ("alias", "80001000"), ("new", "80002000")):
                    self.assertIn(f"{name} 0x{address}", first)
                self.assertIn("entry 0x80000000 unit", first)
                self.assertTrue(incremental.advance(project, "us", root, "before", "after"))
                self.assertEqual((root / "symbol-addresses.txt").read_text(), first)
            for name in ("undefined_funcs_auto.txt", "undefined_syms_auto.txt"):
                self.assertNotIn("new =", (root / name).read_text())
                self.assertIn("untouched =", (root / name).read_text())
            self.assertIn("PROVIDE(alias = 0x80001000);", (root / "committed_symbols.ld").read_text())

    def test_reconciles_without_new_bindings(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            symbols = root / "symbols.txt"
            symbols.write_text("old = 0x80001000;\n")
            project = SimpleNamespace(version=lambda version: SimpleNamespace(symbols=symbols))
            (root / "committed_symbols.ld").write_text("PROVIDE(old = 0x80001000);\n")
            (root / "symbol-addresses.txt").write_text("")
            with patch("unbake.match.relink.retarget_rows", return_value=True):
                self.assertTrue(incremental.advance(project, "us", root, "before", "after"))
            self.assertEqual((root / "symbol-addresses.txt").read_text(), "old 0x80001000\n")
