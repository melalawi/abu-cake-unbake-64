"""Ownership changes reuse extraction, including folded contiguous entries."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.decomp.support import fixture
from unbake.match import incremental
from unbake.match.relink import retarget_rows
from unbake import config
from unbake.project import build, makefile
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

    def test_unchanged_split_with_stale_published_inventory_refuses_before_rebuild(self):
        with tempfile.TemporaryDirectory() as directory:
            project, generation, before = self._retained(Path(directory))
            after = before.replace("asm, nonmatchings/alpha", "c, alpha")
            project.version("us").split.write_text(after)
            with patch.object(incremental, "advance") as advance, self.assertRaises(config.Held) as refusal:
                incremental._prepare_version((project, project, {"us": generation}, {"us": after}), "us")
            advance.assert_not_called()
            self.assertIn("retained extraction omits published C: alpha", refusal.exception.reason)

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


class CompileCancellationTests(unittest.TestCase):
    def test_first_diagnostic_cancels_queued_versions_and_reports_source(self):
        with tempfile.TemporaryDirectory() as directory:
            project, policy, _ = fixture(Path(directory), versions=("us", "eu"), case=self)
            policy.cores = 1
            recipe = makefile.description(project)
            (project.tools / "build.json").write_text(json.dumps(recipe))
            sources = [project.src / f"unit{index}.c" for index in range(20)]
            generations = {v: project.build / (v + ".proof") for v in project.versions}
            splits = {v: project.version(v).split.read_text() for v in project.versions}
            visited = []

            def compile_chunk(selected, selected_policy, members, version, out, *, cancel_file):
                self.assertFalse(cancel_file.exists())
                visited.append((version, list(members)))
                return {members[0].stem: "redefinition of M2C_UNK"}

            with (
                patch.object(incremental, "advance", return_value=True),
                patch.object(incremental, "changed_sources", return_value=sources),
                patch.object(build, "compile_objects", side_effect=compile_chunk),
                self.assertRaises(config.Held) as refused,
            ):
                incremental.prepare(project, project, policy, generations, splits, submitted={"unit19"})
            self.assertEqual(visited, [("us", sources[:10])])
            self.assertEqual(refused.exception.phase, "match")
            self.assertIn("submit.dependencies: VERSION us:", refused.exception.reason)
            self.assertIn(str(sources[0]), refused.exception.reason)
            self.assertIn("redefinition of M2C_UNK", refused.exception.reason)
            self.assertFalse(list(generations["us"].rglob(".cancel-*")))

    def test_submitted_compile_failures_return_source_faults_on_every_version(self):
        with tempfile.TemporaryDirectory() as directory:
            project, policy, _ = fixture(Path(directory), versions=("us", "eu"), case=self)
            (project.tools / "build.json").write_text(json.dumps(makefile.description(project)))
            source = project.src / "alpha.c"
            generations = {v: project.build / (v + ".proof") for v in project.versions}
            splits = {v: project.version(v).split.read_text() for v in project.versions}
            for generation in generations.values():
                generation.mkdir(parents=True, exist_ok=True)
                (generation / "symbol-addresses.txt").write_text("")
                (generation / f"{project.name}.ld").write_text("")

            def compile_versions(selected, selected_policy, jobs, *, stop_on_error=False):
                self.assertIs(selected, project)
                self.assertIs(selected_policy, policy)
                self.assertEqual(set(jobs), set(project.versions))
                for version, (sources, out) in jobs.items():
                    self.assertEqual(list(sources), [] if stop_on_error else [source])
                    self.assertEqual(out, generations[version] / "obj/src")
                return {v: {} if stop_on_error else {"alpha": f"broken on {v}"} for v in jobs}

            with (
                patch.object(incremental, "advance", return_value=True),
                patch.object(incremental, "changed_sources", return_value=[source]),
                patch.object(build, "compile_versions", side_effect=compile_versions) as compile_pool,
                patch.object(incremental, "Object") as objects,
            ):
                faults = incremental.prepare(project, project, policy, generations, splits, submitted={"alpha"})
            self.assertEqual(
                faults, {"alpha": ["us: compile diagnostic: broken on us", "eu: compile diagnostic: broken on eu"]}
            )
            self.assertEqual(compile_pool.call_count, 2)
            objects.assert_not_called()
