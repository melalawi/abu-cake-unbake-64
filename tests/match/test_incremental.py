"""Ownership changes reuse extraction, including folded contiguous entries."""

import json
import tempfile
import unittest
from pathlib import Path

from tests.decomp.support import fixture
from unbake.match.relink import retarget_rows
from unbake.project_tools.extract import unit_ranges


class RetargetTests(unittest.TestCase):
    def _retained(self, directory):
        project, _, _ = fixture(directory, case=self)
        generation = project.build / "us.generation"
        before = project.version("us").split.read_text()
        paths = ["nonmatchings/alpha", "beta", "gamma"]
        (generation / f"{project.name}.ld").write_text(
            "SECTIONS { .text : {\n"
            + "".join(f"obj/asm/{name}.o(.text);\n" for name in paths)
            + "} .bss (NOLOAD) : {\n"
            + "".join(f"obj/asm/{name}.o(.bss);\n" for name in paths)
            + "} }\n"
        )
        (generation / ".split.mk").write_text(
            "C_OBJECTS := \nASM_OBJECTS := "
            + " ".join(f"$(BUILD)/obj/asm/{name}.o" for name in paths)
            + "\nASSET_OBJECTS := \n"
        )
        (generation / "unit-ranges.json").write_text(json.dumps(unit_ranges(before)))
        (generation / "splat_symbols.csv").write_text("vram_start,name,type\n80001000,alpha,asm\n8000100C,beta,asm\n")
        for name in paths:
            obj = generation / f"obj/asm/{name}.o"
            obj.parent.mkdir(parents=True, exist_ok=True)
            obj.touch()
        return project, generation, before

    def test_folded_entries_transfer_and_restore_without_extraction(self):
        with tempfile.TemporaryDirectory() as directory:
            project, generation, before = self._retained(Path(directory))
            graph = generation / ".split.mk"
            graph.write_text(graph.read_text().replace("C_OBJECTS := \n", "C_OBJECTS :=\n"))
            after = "".join(line for line in before.splitlines(keepends=True) if "asm, beta" not in line)
            after = after.replace("asm, nonmatchings/alpha", "c, alpha")
            self.assertTrue(retarget_rows(project, generation, before, after))
            script = (generation / f"{project.name}.ld").read_text()
            self.assertIn("obj/src/alpha.o(.text)", script)
            self.assertNotIn("obj/asm/beta.o", script)
            self.assertEqual(json.loads((generation / "unit-ranges.json").read_text())["alpha"]["end"], 0x58)
            self.assertTrue(retarget_rows(project, generation, after, before))
            script = (generation / f"{project.name}.ld").read_text()
            self.assertIn("obj/asm/nonmatchings/alpha.o(.text)", script)
            self.assertIn("obj/asm/beta.o(.text)", script)
            self.assertIn("obj/asm/beta.o(.bss)", script)
            self.assertNotIn("obj/src/alpha.o", script)
            self.assertEqual(json.loads((generation / "unit-ranges.json").read_text()), {})

    def test_changed_placement_refuses_without_writing_retained_files(self):
        with tempfile.TemporaryDirectory() as directory:
            project, generation, before = self._retained(Path(directory))
            files = [
                generation / name
                for name in (f"{project.name}.ld", ".split.mk", "unit-ranges.json", "splat_symbols.csv")
            ]
            snapshot = [path.read_bytes() for path in files]
            after = before.replace("[0x40, asm, nonmatchings/alpha]", "[0x44, c, alpha]")
            self.assertFalse(retarget_rows(project, generation, before, after))
            self.assertEqual([path.read_bytes() for path in files], snapshot)
