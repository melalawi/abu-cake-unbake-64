"""Missing per-version data references retain ROM-proved symbol definitions."""

import struct
import tempfile
import unittest
from pathlib import Path

from tests.decomp.support import assemble, fixture
from unbake.match import data_symbols
from unbake.project_tools.elf import Object


class DataSymbolTests(unittest.TestCase):
    def test_only_used_unplaced_external_data_requires_an_object(self) -> None:
        source = """
extern int placed, unused, table[];
extern int (*callback)(int), function(int);
int alpha(void) {
    const char *label = "unused";
    /* unused and function are not data references. */
    return placed + table[0] + callback(1) + function(2);
}
"""
        self.assertEqual(data_symbols.unresolved(source, {"placed"}), {"table", "callback"})
        self.assertEqual(data_symbols.unresolved(source, {"placed", "table", "callback"}), set())

    def test_signed_pairs_and_addends_publish_in_each_owning_version(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            source = ".set noreorder\n.text\n.globl alpha\nalpha:\nlui $8,%hi(table+4)\naddiu $8,$8,%lo(table+4)\n"
            obj = assemble(root, "alpha", source)
            words = [0x3C08800F, 0x25088194]
            project, _policy, _ = fixture(root, words, ("us", "eu"), case=self)
            for version in project.versions:
                found = data_symbols.needs(project, "alpha", version, obj)
                self.assertEqual(
                    [(need.version, need.name, need.address) for need in found], [(version, "table", 0x800E8190)]
                )
            self.assertEqual(data_symbols.placements(Object(obj), struct.pack(">II", *words), {"table"}), {})

    def test_disagreeing_references_and_changed_instructions_refuse_placement(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            obj = Object(
                assemble(
                    root,
                    "alpha",
                    ".set noreorder\n.text\nlui $8,%hi(table)\n"
                    "addiu $8,$8,%lo(table)\nlui $8,%hi(table)\naddiu $8,$8,%lo(table)\n",
                )
            )
            with self.assertRaisesRegex(ValueError, "conflicting.*placements"):
                data_symbols.placements(
                    obj, struct.pack(">IIII", 0x3C08800E, 0x25081234, 0x3C08800E, 0x25085678), set()
                )
            with self.assertRaisesRegex(ValueError, "instruction differs"):
                data_symbols.placements(
                    obj, struct.pack(">IIII", 0x3C09800E, 0x25081234, 0x3C08800E, 0x25081234), set()
                )
