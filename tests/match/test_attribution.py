"""Compiler and multi-line linker diagnostics identify owning batch inputs."""

import tempfile
import unittest
from pathlib import Path

from tests.decomp.support import fixture
from unbake.match.attribution import diagnose


class AttributionTests(unittest.TestCase):
    def test_multiline_link_error_names_current_owner_and_candidate_provider(self):
        with tempfile.TemporaryDirectory() as directory:
            project, _, _ = fixture(Path(directory))
            generation = project.build / "us.generation"
            (generation / "build.log").write_text(
                "ld: obj/src/alpha.o: in function `constant':\n"
                "source.i:(.unbake_pool_80003000+0x0): multiple definition of `no symbol'; "
                "obj/src/beta.o:(.unbake_pool_80003004+0x0): first defined here\n"
                "ld: obj/src/existing.o: undefined reference to `missing'\n"
            )
            faults = diagnose(project, ["us"], {"us": generation}, {"alpha", "beta", "gamma"})
            self.assertEqual(set(faults), {"alpha", "beta"})
            self.assertEqual(len(faults["alpha"]), 1)
            self.assertIn("multiple definition", faults["alpha"][0])

    def test_chunk_compile_failure_names_source_without_linked_image(self):
        with tempfile.TemporaryDirectory() as directory:
            project, _, _ = fixture(Path(directory))
            generation = project.build / "us.generation"
            (generation / "build.log").write_text("HELD(compile): batch objects failed: src/beta.c: invalid C\n")
            self.assertEqual(set(diagnose(project, ["us"], {"us": generation}, {"alpha", "beta"})), {"beta"})

    def test_compiler_lines_follow_the_failed_source_without_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            project, _, _ = fixture(Path(directory))
            generation = project.build / "us.generation"
            (generation / "build.log").write_text(
                "HELD(compile): batch objects failed:\n"
                "/abs/build/src/beta.c: /abs/tools/cc1 exited 33: source.i: In function `beta':\n"
                "/abs/build/obj/src/.object-x/source.i:12: structure has no member named `value'\n"
                "make: *** [build] Error 1\n"
            )
            faults = diagnose(project, ["us"], {"us": generation}, {"alpha", "beta"})
            self.assertEqual(set(faults), {"beta"})
            self.assertIn("source.i:12: structure has no member named `value'", faults["beta"][0])
            self.assertNotIn("/abs/", faults["beta"][0])
