"""decomp.original_asm: the record, the guard, the .s text, and the land, build and progress paths around them."""

import json
import struct
from unittest import mock

from tests import project_fixture
from tests.kit import TempCase
from unbake import buildfiles, config, land
from unbake.config import Held
from unbake.decomp import original_asm
from unbake.layout import split
from unbake.process import named
from unbake.report import progress
from unbake.work import shape

MTC0 = [0x40846000, 0x03E00008, 0]  # mtc0 a0,$12; jr ra; nop (__osSetSR)
PLAIN = struct.pack(">3I", 0x24020002, 0x03E00008, 0)  # li v0,2; jr ra; nop


class OriginalAsmCase(TempCase):
    def setUp(self) -> None:
        super().setUp()
        self.project, self.host = project_fixture.make(self.root, MTC0, versions=("us", "eu"))

    def hasm(self, name: str) -> None:
        for version in self.project.versions:
            path = self.project.version(version).split
            path.write_text(path.read_text().replace(f"asm, {name}]", f"hasm, {name}]"))

    def source(self, name: str) -> None:
        (self.project.src / f"{name}.s").write_text(f".globl {name}\n{name}:\n")

    def record(self, **rules: str) -> None:
        functions = {name: {"rule": rule} for name, rule in rules.items()}
        (self.project.root / original_asm.MANIFEST).write_text(json.dumps({"schema": 1, "functions": functions}))

    def guard(self) -> None:
        original_asm.guard(config.load(self.project.root))


class GuardTests(OriginalAsmCase):
    def test_a_recorded_proved_function_passes(self) -> None:
        self.hasm("alpha")
        self.source("alpha")
        self.record(alpha="cop0")
        self.guard()

    def test_refusals(self) -> None:
        cases = {
            ".s with no record": (lambda: self.source("beta"), "original.unrecorded"),
            "hasm row with no record": (lambda: (self.hasm("alpha"), self.source("alpha")), "original.unrecorded"),
            "record naming a rule the bytes do not prove": (
                lambda: (self.hasm("alpha"), self.source("alpha"), self.record(alpha="fcsr")),
                "original.rule",
            ),
            "record on bytes that prove no rule": (
                lambda: (self.hasm("beta"), self.source("beta"), self.record(beta="cop0")),
                "original.not_original",
            ),
            "unknown rule name": (
                lambda: (self.hasm("alpha"), self.source("alpha"), self.record(alpha="hard")),
                "'hard' is not one of cop0, fcsr, isa, kreg",
            ),
            "record without its hasm row": (lambda: (self.source("alpha"), self.record(alpha="cop0")), "original.row"),
            "hasm row without its source": (
                lambda: (self.hasm("alpha"), self.record(alpha="cop0")),
                "original.source",
            ),
        }
        for label, (arrange, reason) in cases.items():
            with self.subTest(label):
                self.setUp()
                arrange()
                with self.assertRaises(Held) as refused:
                    self.guard()
                self.assertIn(reason, refused.exception.reason)

    def test_a_record_holds_only_the_rule(self) -> None:
        (self.project.root / original_asm.MANIFEST).write_text(
            json.dumps({"schema": 1, "functions": {"alpha": {"rule": "cop0", "library": "libultra"}}})
        )
        with self.assertRaises(Held) as refused:
            original_asm.load(self.project)
        self.assertIn("expected {rule}", refused.exception.reason)


class LandTests(OriginalAsmCase):
    def test_bytes_a_compiler_can_emit_never_land_as_asm(self) -> None:
        # beta is ordinary C (hard, 0% or unmatched drafts change nothing): refused before any write.
        with self.assertRaises(Held) as refused:
            land.land_original(self.project, self.host, "beta")
        self.assertIn("original.not_original", refused.exception.reason)
        self.assertFalse((self.project.src / "beta.s").exists())
        self.assertFalse((self.project.root / original_asm.MANIFEST).exists())

    def test_every_holding_version_must_prove_the_rule(self) -> None:
        real = split.words

        def words(project: config.Project, row: split.Function) -> bytes:
            return PLAIN if row.version == "eu" else real(project, row)

        with mock.patch.object(split, "words", words), self.assertRaises(Held) as refused:
            land.land_original(self.project, self.host, "alpha")
        self.assertIn("eu alpha: no original-asm rule holds", refused.exception.reason)
        self.assertFalse((self.project.src / "alpha.s").exists())

    def test_only_an_unlanded_asm_row_lands(self) -> None:
        self.hasm("alpha")
        with self.assertRaises(Held) as refused:
            land.land_original(config.load(self.project.root), self.host, "alpha")
        self.assertIn("land.original_kind", refused.exception.reason)


class BuildAndProgressTests(OriginalAsmCase):
    def test_a_hasm_row_is_a_unit_built_from_its_source(self) -> None:
        self.hasm("alpha")
        with self.assertRaises(Held) as refused:
            buildfiles.slices_mk(config.load(self.project.root), "us")
        self.assertIn("has no src/alpha.s", refused.exception.reason)
        self.source("alpha")
        project = config.load(self.project.root)
        text = buildfiles.slices_mk(project, "us")
        self.assertIn("build/us/hasm/alpha.bin", text)
        self.assertIn("us.U.alpha := 0x80001000:0x40:0xC", text)
        makefile = buildfiles.makefile(project, self.host)
        self.assertIn("build/$1/hasm/%.bin: src/%.s", makefile)
        self.assertIn("HASM_ASFLAGS := -EB -mips2 -G0", makefile)
        # A new unit shortens the slice before it: slices are cut again when slices.mk changes.
        self.assertIn("build/$1/slices/%.bin: $$($1.BASEROM) versions/$1/slices.mk", makefile)

    def test_original_asm_is_only_denominator_in_its_own_category(self) -> None:
        rows = {
            "c": ("c", ".c", True),
            "hasm": ("original_asm", ".s", False),
            "asm": ("asm", None, False),
        }
        for kind, (category, suffix, complete) in rows.items():
            with self.subTest(kind):
                unit = progress._unit(split.Function("us", "alpha", 0, 12, 0, "alpha", kind, ("alpha",)))
                self.assertEqual(unit["metadata"]["complete"], complete)
                self.assertEqual(unit["metadata"].get("progress_categories"), [category] if category else None)
                if suffix:
                    self.assertEqual(unit["metadata"]["source_path"], f"src/alpha{suffix}")


class SourceTextTests(OriginalAsmCase):
    FOUND = shape.Original("cop0", "mtc0 at +0x0")

    def test_spelling(self) -> None:
        data = struct.pack(">5I", 0x40846000, 0x0C000010, 0x1000FFFE, 0x44C4F800, 0)
        lines = ["mtc0 a0,$12", "jal 0x80000040", "b 0x80001004", "ctc1 a0,c1_fcsr", "nop"]
        instructions, labels = original_asm.body(data, 0x80001000, lines)
        self.assertEqual(instructions[0], ("mtc0 $a0,$12", ""))
        self.assertEqual(instructions[1], (".word 0x0C000010", "jal 0x80000040"))
        self.assertEqual(instructions[2], ("b .L4", ""))
        self.assertEqual(instructions[3], ("ctc1 $a0,$31", ""))
        self.assertEqual(labels, {4})
        text, positions = original_asm.render("alpha", self.FOUND, instructions, labels)
        self.assertIn("# alpha: original asm (cop0: mtc0 at +0x0).", text)
        self.assertIn(".L4:\n    jal", text.replace(".word 0x0C000010  # ", ""))
        self.assertEqual(sorted(positions.values()), [0, 1, 2, 3, 4])

    def test_lines_gas_rejects_or_encodes_otherwise_fall_back_to_words(self) -> None:
        project = config.load(self.project.root)
        row = split.functions(project, "us")[0]
        data = struct.pack(">3I", *MTC0)
        lines = ["mtc0 a0,$12", "jr ra", "nop"]
        cases = {
            "gas rejects line 9": [
                Held(
                    named(
                        "fixture.refusal", "as exited 1: alpha.s:9: Error: bad", owner="fixture", stage="original-asm"
                    )
                ),
                data,
            ],
            "gas encodes word 1 otherwise": [data[:4] + b"\0\0\0\0" + data[8:], data],
        }
        for label, results in cases.items():
            with self.subTest(label):
                with (
                    mock.patch.object(original_asm, "_disassemble", return_value=lines),
                    mock.patch.object(original_asm, "assemble", side_effect=results),
                ):
                    text = original_asm.write_source(project, self.host, row, data, self.FOUND)
                self.assertIn(".word 0x03E00008  # jr $ra", text)

    def test_a_text_that_never_assembles_to_the_row_is_refused(self) -> None:
        project = config.load(self.project.root)
        row = split.functions(project, "us")[0]
        data = struct.pack(">3I", *MTC0)
        with (
            mock.patch.object(original_asm, "_disassemble", return_value=["mtc0 a0,$12", "jr ra", "nop"]),
            mock.patch.object(original_asm, "assemble", return_value=data + b"\0\0\0\0"),
            self.assertRaises(Held) as refused,
        ):
            original_asm.write_source(project, self.host, row, data, self.FOUND)
        self.assertIn("original.mismatch", refused.exception.reason)
