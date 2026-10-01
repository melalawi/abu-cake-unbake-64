"""Cold trial targets use the standalone Makefile without linking a ROM."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.project.makefile_fixture import executable, fixture, write_rendered
from unbake.decomp.trial_target import make_target, target_object
from unbake.project import build
from unbake.project.config import Held


class TrialTargetTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.addCleanup(patch.stopall)
        self.root = Path(temporary.name)
        self.project, _policy = fixture(self.root)
        write_rendered(self.project)

    def test_missing_generation_builds_inventory_and_only_requested_assembly_object(self) -> None:
        self.assertFalse((self.root / "build").exists())
        target = target_object(self.project, "first", "us")
        generation = build.current_generation(self.project, "us")
        self.assertEqual(target, generation / "obj/asm/first.o")
        self.assertEqual(target.read_bytes(), b"A")
        self.assertTrue((generation / "game.ld").is_file())
        self.assertTrue((generation / ".split.mk").is_file())
        self.assertEqual(list(generation.rglob("*.o")), [target])
        self.assertEqual((self.root / "calls").read_text().splitlines(), ["splat", "as"])
        self.assertFalse(list(generation.glob("*.elf")))
        self.assertFalse(list(generation.glob("*.z64")))

    def test_missing_c_object_builds_only_its_unit_and_recovers_leftover_receipt(self) -> None:
        target = target_object(self.project, "middle", "us")
        original = target.read_bytes()
        self.assertEqual((self.root / "calls").read_text().splitlines(), ["splat", "cc"])
        self.assertEqual(list(target.parents[2].rglob("*.o")), [target])
        target.unlink()
        self.assertTrue(target.with_suffix(".built").is_file())
        self.assertEqual(target_object(self.project, "middle", "us").read_bytes(), original)
        # The compiler cache can restore the missing object without invoking cc.
        self.assertEqual((self.root / "calls").read_text().splitlines(), ["splat", "cc"])

    def test_existing_target_does_not_run_make(self) -> None:
        target = target_object(self.project, "first", "us")
        with patch("unbake.decomp.trial_target.subprocess.run") as run:
            self.assertEqual(target_object(self.project, "first", "us"), target)
        run.assert_not_called()

    def test_c_target_does_not_build_other_cold_c_units(self) -> None:
        (self.root / "src/unused.c").write_text("B")
        make_target(self.project, "us", Path("build/us/game.ld"))
        generation = build.current_generation(self.project, "us")
        graph = generation / ".split.mk"
        graph.write_text(graph.read_text() + "C_OBJECTS += $(BUILD)/obj/src/unused.o\n")
        target = target_object(self.project, "middle", "us")
        self.assertEqual(list(generation.rglob("*.o")), [target])
        self.assertEqual((self.root / "calls").read_text().splitlines(), ["splat", "cc"])

    def test_inventory_build_failure_quotes_target_and_stderr(self) -> None:
        executable(self.root / "tools/splat", "import sys\nsys.stderr.write('inventory unavailable\\n')\nsys.exit(3)\n")
        with self.assertRaises(Held) as failure:
            target_object(self.project, "first", "us")
        self.assertEqual(failure.exception.phase, "try")
        self.assertIn("make -j4 VERSION=us C_COLD= build/us/game.ld", failure.exception.reason)
        self.assertIn("inventory unavailable", failure.exception.reason)
        self.assertFalse(list((self.root / "build").rglob("*.o")))

    def test_object_build_failure_quotes_target_and_stderr(self) -> None:
        target_object(self.project, "middle", "us")
        executable(self.root / "tools/as", "import sys\nsys.stderr.write('assembler unavailable\\n')\nsys.exit(7)\n")
        with self.assertRaises(Held) as failure:
            target_object(self.project, "first", "us")
        self.assertEqual(failure.exception.phase, "try")
        self.assertIn("make -j4 VERSION=us C_COLD= build/us/obj/asm/first.o", failure.exception.reason)
        self.assertIn("assembler unavailable", failure.exception.reason)

    def test_inventory_does_not_fall_back_to_objdiff_wrappers(self) -> None:
        target = target_object(self.project, "first", "us")
        generation = build.current_generation(self.project, "us")
        (generation / "game.ld").write_text("SECTIONS {}\n")
        (generation / "objdiff.json").write_text('{"units": [{"name": "first", "target_path": "wrapper.o"}]}')
        (generation / "wrapper.o").write_bytes(target.read_bytes())
        with self.assertRaisesRegex(Held, "obj/asm/first.o.*absent"):
            target_object(self.project, "first", "us")
