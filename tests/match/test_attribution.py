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


class PlacementTests(unittest.TestCase):
    def _objects(self, directory, *, short=True, bad=False):
        from tests.decomp.support import assemble, assembly

        project, _, _ = fixture(directory)
        generation = project.build / "us.generation"
        source = generation / "obj/src"
        source.mkdir()
        project.version("us").split.write_text(
            project.version("us")
            .split.read_text()
            .replace("asm, nonmatchings/alpha", "c, alpha")
            .replace("asm, beta", "c, beta")
            .replace("asm, gamma", "c, gamma")
        )
        for name, words in (
            ("alpha", [0x24020001, 0x03E00008] if short else [0x24020001, 0x03E00008, 0]),
            ("beta", [0x24020002, 0x03E00008, 0]),
            ("gamma", [0x24020004 if bad else 0x24020003, 0x03E00008, 0]),
        ):
            assemble(source, name, assembly(name, words))
        (generation / "build.log").write_text("")
        return project, generation

    def _link(self, project, generation, body=None):
        import subprocess

        from tests.decomp.support import LINKER

        script = generation / "proof.ld"
        script.write_text(
            "SECTIONS { .text 0x80001000 : SUBALIGN(1) { "
            + (body or "obj/src/alpha.o(.text) obj/src/beta.o(.text) obj/src/gamma.o(.text)")
            + " } /DISCARD/ : { *(.reginfo) *(.MIPS.abiflags) *(.pdr) } }"
        )
        subprocess.run(
            [LINKER, "-T", script.name, "-Map", f"{project.name}.map", "-o", f"{project.name}.elf"],
            cwd=generation,
            check=True,
            capture_output=True,
        )

    def test_short_origin_does_not_hold_later_placement_victims(self):
        with tempfile.TemporaryDirectory() as directory:
            project, generation = self._objects(Path(directory))
            self._link(project, generation)
            faults = diagnose(project, ["us"], {"us": generation}, {"alpha", "beta", "gamma"})
            self.assertEqual(set(faults), {"alpha"})
            self.assertIn("shift origin", "; ".join(faults["alpha"]))
            self.assertIn("target span 12", "; ".join(faults["alpha"]))

    def test_shift_does_not_hide_independent_byte_fault(self):
        with tempfile.TemporaryDirectory() as directory:
            project, generation = self._objects(Path(directory), bad=True)
            self._link(project, generation)
            faults = diagnose(project, ["us"], {"us": generation}, {"alpha", "beta", "gamma"})
            self.assertEqual(set(faults), {"alpha", "gamma"})
            self.assertIn("produced 24020004", "; ".join(faults["gamma"]))

    def test_compile_failure_does_not_hide_bindings_and_bytes_without_elf(self):
        from tests.decomp.support import assemble

        with tempfile.TemporaryDirectory() as directory:
            project, generation = self._objects(Path(directory), short=False, bad=True)
            (generation / "build.log").write_text("src/alpha.c: invalid C\n")
            assemble(
                generation / "obj/src", "beta", ".set noreorder\n.text\n.globl beta\nbeta:\njal missing\nnop\nnop\n"
            )
            faults = diagnose(project, ["us"], {"us": generation}, {"alpha", "beta", "gamma"})
            self.assertEqual(set(faults), {"alpha", "beta", "gamma"})
            self.assertIn("compile diagnostic", faults["alpha"][0])
            self.assertIn("undefined reference to missing", "; ".join(faults["beta"]))
            self.assertIn("produced 24020004", "; ".join(faults["gamma"]))

    def test_explicit_tail_alignment_can_absorb_short_text(self):
        with tempfile.TemporaryDirectory() as directory:
            project, generation = self._objects(Path(directory))
            path = project.version("us").split
            path.write_text(path.read_text().replace("[0x40, c, alpha]", "[0x40, c, alpha, {align: 4}]"))
            # Eight emitted bytes plus a declared twelve-byte aligned interval.
            # Use an address four bytes below a sixteen-byte boundary.
            path.write_text(
                path.read_text().replace("vram: 0x80001000", "vram: 0x80001004").replace("{align: 4}", "{align: 16}")
            )
            self.assertEqual(diagnose(project, ["us"], {"us": generation}, {"alpha"}), {})

    def test_data_span_origin_does_not_hold_shifted_text(self):
        from tests.decomp.support import assemble, assembly

        with tempfile.TemporaryDirectory() as directory:
            project, generation = self._objects(Path(directory), short=False)
            path = project.version("us").split
            path.write_text(
                path.read_text()
                .replace("      - [0x4C, c, beta]", "      - [0x4C, data, alpha]\n      - [0x58, c, beta]")
                .replace("[0x58, c, gamma]", "[0x64, c, gamma]")
                .replace("[0x64]", "[0x70]")
            )
            image = project.version("us").baserom
            image.write_bytes(image.read_bytes()[:0x4C] + bytes(12) + image.read_bytes()[0x4C:])
            assemble(
                generation / "obj/src",
                "alpha",
                assembly("alpha", [0x24020001, 0x03E00008, 0]) + ".section .data\n.word 0\n",
            )
            self._link(
                project,
                generation,
                "obj/src/alpha.o(.text) obj/src/alpha.o(.data) obj/src/beta.o(.text) obj/src/gamma.o(.text)",
            )
            faults = diagnose(project, ["us"], {"us": generation}, {"alpha", "beta", "gamma"})
            self.assertEqual(set(faults), {"alpha"})
            self.assertIn(".data: consumed 4 bytes, target span 12", "; ".join(faults["alpha"]))

    def test_automatic_bss_growth_uses_proved_original_providers(self):
        from tests.decomp.support import assemble, assembly

        with tempfile.TemporaryDirectory() as directory:
            project, generation = self._objects(Path(directory), short=False)
            reference = Path(directory) / "reference"
            reference.mkdir()
            original, _, _ = fixture(reference)
            baseline = original.build / "us.generation"
            (generation / f"{project.name}.ld").write_text("SECTIONS { .bss : { obj/src/alpha.o(.bss) } }")
            assemble(
                generation / "obj/src",
                "alpha",
                assembly("alpha", [0x24020001, 0x03E00008, 0]) + ".section .bss\n.space 8\n",
            )
            faults = diagnose(project, ["us"], {"us": generation}, {"alpha"}, (original, {"us": baseline}))
            self.assertEqual(set(faults), {"alpha"})
            self.assertIn(".bss: size 8, target span 0", "; ".join(faults["alpha"]))
