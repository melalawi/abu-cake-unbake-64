"""Missing per-version data references retain ROM-proved symbol definitions."""

import struct
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from tests.decomp.support import assemble, fixture
from unbake.match import data_symbols
from unbake.objects.elf import Object


class DataSymbolTests(unittest.TestCase):
    def identity_project(self, root, source, target):
        paths = {version: root / f"{version}.txt" for version in ("source", "target")}
        paths["source"].write_text(source)
        paths["target"].write_text(target)
        return SimpleNamespace(versions=("source", "target"), version=lambda v: SimpleNamespace(symbols=paths[v]))

    def test_missing_target_alias_has_identity_between_agreeing_data_anchors(self):
        with tempfile.TemporaryDirectory() as temporary:
            project = self.identity_project(
                Path(temporary),
                "before = 0x80001000;\nshared = 0x80001010;\nafter = 0x80001020;\n",
                "before = 0x80002000;\nafter = 0x80002020;\n",
            )
            self.assertEqual(data_symbols.preferred_address(project, "target", "shared"), 0x80002010)
            self.assertEqual(data_symbols.preferred_address(project, "source", "shared"), 0x80001010)

    def test_disagreeing_or_missing_anchors_do_not_supply_identity(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for target in ("before = 0x80002000;\n", "before = 0x80002000;\nafter = 0x80002030;\n"):
                with self.subTest(target=target):
                    project = self.identity_project(
                        root,
                        "before = 0x80001000;\nshared = 0x80001010;\nafter = 0x80001020;\n",
                        target,
                    )
                    self.assertIsNone(data_symbols.preferred_address(project, "target", "shared"))

    def test_functions_do_not_supply_data_anchors(self):
        with tempfile.TemporaryDirectory() as temporary:
            project = self.identity_project(
                Path(temporary),
                "before = 0x80001000; // type:func\nshared = 0x80001010;\nafter = 0x80001020;\n",
                "before = 0x80002000;\nafter = 0x80002020;\n",
            )
            self.assertIsNone(data_symbols.preferred_address(project, "target", "shared"))

    def test_unknown_spelling_does_not_supply_identity(self):
        with tempfile.TemporaryDirectory() as temporary:
            project = self.identity_project(Path(temporary), "", "")
            self.assertIsNone(data_symbols.preferred_address(project, "target", "D_80001000_other"))

    def test_conflicting_version_identities_do_not_break_a_tie(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project = self.identity_project(
                root,
                "before = 0x80001000;\nshared = 0x80001010;\nafter = 0x80001020;\n",
                "before = 0x80002000;\nafter = 0x80002020;\n",
            )
            third = root / "third.txt"
            third.write_text("before = 0x80003000;\nshared = 0x80003014;\nafter = 0x80003020;\n")
            original_version = project.version
            project.versions = (*project.versions, "third")
            project.version = lambda v: SimpleNamespace(symbols=third) if v == "third" else original_version(v)
            self.assertIsNone(data_symbols.preferred_address(project, "target", "shared"))

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
