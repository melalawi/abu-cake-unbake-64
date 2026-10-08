"""Unproved compiler constants can be scored, while strict links require resident proof."""

import struct
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from tests.elf_fixture import linked_fixture, write_object
from tests.project_fixture import ProjectCase
from unbake import atomic, buildfiles, runner
from unbake.config import Held
from unbake.objects import rodata
from unbake.objects.elf import Object
from unbake.work.score import measure_words


class UnresolvedSectionsTests(unittest.TestCase):
    def test_only_reachable_constant_sections_and_their_dependencies(self):
        with TemporaryDirectory() as temporary:
            obj = write_object(
                Path(temporary) / "unit.o",
                {".text": bytes(4), ".rdata": bytes(4), ".lit8": bytes(8), ".unused": bytes(4)},
                [("pool", ".rdata", 0, 0, 3), ("dependency", ".lit8", 0, 0, 3), ("absolute", "ABS", 0, 0)],
                relocations=[(".text", 0, 2, "pool"), (".rdata", 0, 2, "dependency")],
            )
            parsed = Object(obj)
            index = parsed.section(".rdata")
            parsed.sections[index][2] = 0  # GCC pools can lack SHF_ALLOC.
            self.assertEqual(rodata.unresolved_sections(parsed), [".rdata", ".lit8"])

    def test_no_text_relocation_means_no_overlay(self):
        with TemporaryDirectory() as temporary:
            obj = write_object(Path(temporary) / "unit.o", {".text": bytes(4), ".rodata": bytes(8)})
            self.assertEqual(rodata.unresolved_sections(Object(obj)), [])

    def test_overlay_precedes_discard_and_has_no_resident_address_claim(self):
        script = "SECTIONS\n{\n  .text : { *(.text) }\n  /DISCARD/ : { *(*) }\n}\n"
        result = rodata.insert_fragment(script, rodata.trial_fragment([".rdata", ".rodata.cst8"]))
        self.assertLess(result.index(".trial_0 0 (NOLOAD)"), result.index("/DISCARD/"))
        self.assertIn('*(".rodata.cst8")', result)
        with self.assertRaises(ValueError):
            rodata.trial_fragment(['.rodata") }'])


class AlignedBaseTests(unittest.TestCase):
    NOP = 0
    HI, LO = 0x3C010000, 0xC4200000

    def object(self, directory, words, section=".rdata"):
        return Object(
            write_object(
                Path(directory) / "unit.o",
                {".text": struct.pack(f">{len(words)}I", *words), section: bytes.fromhex("4F000000")},
                [("literal", section, 0, 0, 3)],
                relocations=[
                    (".text", 4 * (len(words) - 2), 5, "literal"),
                    (".text", 4 * (len(words) - 1), 6, "literal"),
                ],
            )
        )

    def test_inserted_instruction_does_not_hide_the_pair_base(self):
        # Target has no leading nop, so the pair sits one word later in the candidate than in the ROM.
        target = struct.pack(">II", 0x3C01800C, 0xC42086C0)
        with TemporaryDirectory() as temporary:
            obj = self.object(temporary, [self.NOP, self.HI, self.LO])
            self.assertEqual(rodata.aligned_bases(obj, [".rdata"], target), {".rdata": 0x800B86C0})

    def test_same_offset_pair_and_jump_table_style_section_name(self):
        target = struct.pack(">II", 0x3C01800D, 0xC4208000 | 0x1234)
        with TemporaryDirectory() as temporary:
            obj = self.object(temporary, [self.HI, self.LO], ".rodata")
            self.assertEqual(rodata.aligned_bases(obj, [".rodata"], target), {".rodata": 0x800C9234})

    def test_unmatched_pair_leaves_section_unplaced(self):
        target = struct.pack(">II", 0x00000000, 0x00000000)
        with TemporaryDirectory() as temporary:
            obj = self.object(temporary, [self.HI, self.LO])
            self.assertEqual(rodata.aligned_bases(obj, [".rdata"], target), {})

    def test_trial_fragment_places_inferred_sections_and_zeroes_the_rest(self):
        text = rodata.trial_fragment([".rdata", ".lit8"], {".rdata": 0x800B86C0})
        self.assertIn(".trial_0 0x800B86C0 (NOLOAD)", text)
        self.assertIn(".trial_1 0 (NOLOAD)", text)


class TrialLinkTests(ProjectCase):
    def setUp(self):
        super().setUp()
        self.work = self.root / "trial"
        self.work.mkdir()
        self.script = self.project.root / "versions/us/fixture.ld"
        self.script.write_text(buildfiles.link_script(self.project, "us"))
        (self.script.parent / "symbols.ld").write_text("")
        self.row = SimpleNamespace(name="alpha", address=0x80001000)
        self.source = self.project.work / "alpha/alpha.c"
        self.code = struct.pack(">II", 0x3C010000, 0xC4200004)
        self.seen_scripts = []

    def object(self, section):
        return write_object(
            self.work / "placed.o",
            {".text": self.code, section: bytes.fromhex("3F8000003F000000")},
            [("literal", section, 0, 0, 3)],
            relocations=[(".text", 0, 5, "literal"), (".text", 4, 6, "literal")],
        )

    def native(self, argv, cwd, phase, **kwargs):
        if "-T" in argv:
            script = Path(argv[argv.index("-T") + 1])
            self.seen_scripts.append((script, script.read_text()))
            parsed = Object(Path(argv[-1]))
            sections = rodata.unresolved_sections(parsed)
            linked_fixture(
                Path(argv[argv.index("-o") + 1]),
                [parsed.path],
                {".text": self.row.address, **dict.fromkeys(sections, 0)},
            )
        else:
            Path(argv[-1]).write_bytes(Object(Path(argv[-2])).content(1))
        return ""

    def test_score_keeps_gcc_and_ido_literals_without_modifying_version_script(self):
        original = self.script.read_bytes()
        for section in (".rdata", ".rodata", ".lit8"):
            with self.subTest(section=section), patch.object(runner.process, "run_tool", side_effect=self.native):
                placed = self.object(section)
                result = runner.link(
                    self.project, self.host, placed, "us", self.row, self.work, self.source, score=True
                )
                self.assertEqual(result, self.code)
                self.assertEqual(self.script.read_bytes(), original)
                used, text = self.seen_scripts[-1]
                self.assertEqual(used, self.work / "trial.ld")
                self.assertIn(f'*("{section}")', text)
                self.assertIn("INCLUDE versions/us/symbols.ld", text)

    def test_strict_link_refuses_unplaced_literals_before_invoking_linker(self):
        with patch.object(runner.process, "run_tool") as native, self.assertRaisesRegex(Held, "link.unproved"):
            runner.link(self.project, self.host, self.object(".rodata"), "us", self.row, self.work, self.source)
        native.assert_not_called()

    def test_proved_text_links_using_existing_version_script(self):
        placed = write_object(self.work / "placed.o", {".text": self.code, ".rodata": bytes(8)})
        with patch.object(runner.process, "run_tool", side_effect=self.native):
            result = runner.link(self.project, self.host, placed, "us", self.row, self.work, self.source)
        self.assertEqual(result, self.code)
        self.assertEqual(self.seen_scripts[-1][0], self.script)

    def test_unplaced_pool_cannot_be_exact_even_if_text_matches_and_native_has_no_diagnostic(self):
        obj = self.object(".rdata")

        def place(project, host, original, version, row, output, **kwargs):
            atomic.copyfile(original, output)
            return []

        with (
            patch.object(runner, "place", side_effect=place),
            patch.object(runner.split, "words", return_value=self.code),
            patch.object(runner.process, "run_tool", side_effect=self.native),
        ):
            data, problems = runner.link_function(self.project, self.host, obj, "us", self.row, self.source)
        measured = measure_words("us", self.code, data)
        self.assertTrue(measured.exact)
        self.assertEqual(problems, [".rdata: no proved resident address"])
        measured.typed["relocation"] += len(problems)
        self.assertFalse(measured.exact)

    def test_shifted_reference_links_at_the_aligned_base_not_zero(self):
        shifted = struct.pack(">III", 0, 0x3C010000, 0xC4200000)
        obj = write_object(
            self.work / "placed.o",
            {".text": shifted, ".rdata": bytes.fromhex("4F000000")},
            [("literal", ".rdata", 0, 0, 3)],
            relocations=[(".text", 4, 5, "literal"), (".text", 8, 6, "literal")],
        )
        target = struct.pack(">II", 0x3C01800C, 0xC42086C0)

        def place(project, host, original, version, row, output, **kwargs):
            atomic.copyfile(original, output)
            return [
                ".rdata reference at .text+0x4/0x8: opcode differs from the ROM",
                ".rdata: no proved resident address",
            ]

        with (
            patch.object(runner, "place", side_effect=place),
            patch.object(runner.split, "words", return_value=target),
            patch.object(runner.process, "run_tool", side_effect=self.native),
        ):
            _, problems = runner.link_function(self.project, self.host, obj, "us", self.row, self.source)
        used, text = self.seen_scripts[-1]
        self.assertEqual(used.name, "trial.ld")
        self.assertIn(".trial_0 0x800B86C0 (NOLOAD)", text)
        self.assertIn(".rdata: no proved resident address", problems)
