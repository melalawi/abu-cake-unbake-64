"""Trial checks with isolated project fixtures and real MIPS ELF linking."""

import hashlib
import io
import shutil
import struct
import subprocess
import tempfile
import unittest
from collections.abc import Callable
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import MagicMock, patch

from tests.support import tool
from unbake.decomp import (
    features,
    needs,
    trial,
    trial_artifacts,
    trial_compare,
    trial_compile,
    trial_layout,
    trial_link,
)
from unbake.project import build
from unbake.project.config import Compiler, Held, Policy, Project, Version

SCRATCH_ROOT = Path(tempfile.gettempdir())
ASSEMBLER = cast(Callable[[str], str], tool)("mips-linux-gnu-as")
LINKER = cast(Callable[[str], str], tool)("mips-linux-gnu-ld")
READELF = cast(Callable[[str], str], tool)("mips-linux-gnu-readelf")
OBJDUMP = cast(Callable[[str], str], tool)("mips-linux-gnu-objdump")


def assemble(directory: Path, name: str, text: str) -> Path:
    source = directory / (name + ".s")
    output = directory / (name + ".o")
    source.write_text(text, encoding="utf-8")
    subprocess.run(
        [ASSEMBLER, "-EB", "-mips3", "--no-pad-sections", "-o", str(output), str(source)],
        check=True,
        capture_output=True,
    )
    return output


def assembly(function: str, words: list[int]) -> str:
    return (
        f".set noreorder\n.text\n.balign 4\n.globl {function}\n.type {function}, @function\n{function}:\n"
        + "".join(f".word 0x{word:08X}\n" for word in words)
        + f".size {function}, .-{function}\n"
    )


def fixture(
    directory: Path, words: list[int] | None = None, versions: tuple[str, ...] = ("us",)
) -> tuple[Project, SimpleNamespace, Path]:
    words = [0x24020001, 0x03E00008, 0] if words is None else words
    root = directory / "project"
    root.mkdir()
    for relative in ("src", "include", "asm", "tools", "build"):
        (root / relative).mkdir()
    (root / "include" / "types.h").write_text("typedef int s32;\n", encoding="utf-8")
    version_map = {}
    for v in versions:
        definitions = root / "versions" / v
        definitions.mkdir(parents=True)
        start = 0x80001000 if v == "us" else 0x80202000
        next_offset = 0x40 + len(words) * 4
        split = definitions / "game.yaml"
        split.write_text(
            "name: fixture\nsegments:\n  - [0x0, header, header]\n"
            f"  - name: main\n    type: code\n    start: 0x40\n    vram: 0x{start:X}\n"
            "    subalign: 4\n    subsegments:\n"
            f"      - [0x40, asm, nonmatchings/alpha]\n      - [0x{next_offset:X}, asm, beta]\n"
            f"      - [0x{next_offset + 12:X}, asm, gamma]\n  - [0x{next_offset + 24:X}]\n",
            encoding="utf-8",
        )
        symbols = definitions / "symbol_addrs.txt"
        symbols.write_text(
            f"alpha = 0x{start:X};\nbeta = 0x{start + len(words) * 4:X};\ngamma = 0x{start + len(words) * 4 + 12:X};\n",
            encoding="utf-8",
        )
        baserom = root / f"baserom.{v}.z64"
        all_words = [*words, 0x24020002, 0x03E00008, 0, 0x24020003, 0x03E00008, 0]
        baserom.write_bytes(bytes.fromhex("80371240") + bytes(0x3C) + struct.pack(f">{len(all_words)}I", *all_words))
        generation = root / "build" / f"{v}.generation"
        generation.mkdir()
        program = "\n".join(
            assembly(f, w)
            for f, w in (
                ("alpha", words),
                ("beta", [0x24020002, 0x03E00008, 0]),
                ("gamma", [0x24020003, 0x03E00008, 0]),
            )
        )
        unit = assemble(generation, "original", program)
        script = generation / "layout.ld"
        script.write_text(
            f"external = 0x80003000;\nSECTIONS {{ .text 0x{start:X} : {{ *(.text) }} "
            "/DISCARD/ : { *(.reginfo) *(.MIPS.abiflags) *(.pdr) } }\n",
            encoding="utf-8",
        )
        subprocess.run(
            [LINKER, "-T", str(script), "-o", str(generation / "game.elf"), str(unit)], check=True, capture_output=True
        )
        (root / "build" / v).symlink_to(generation.name, target_is_directory=True)
        version_map[v] = Version(v, baserom, hashlib.sha1(baserom.read_bytes()).hexdigest(), split, symbols, ())
        asm = root / "asm" / v / "nonmatchings"
        asm.mkdir(parents=True)
        (asm / "alpha.s").write_text(assembly("alpha", words), encoding="utf-8")
    compiler = Compiler("ido-7.1", "ido", Path("/compiler/cc"), Path(ASSEMBLER), (), root / "tools" / "compiler.sha256")
    project = Project(
        root,
        "fixture",
        "Fixture",
        versions[0],
        tuple(versions),
        root / "src",
        (root / "include",),
        root / "asm",
        root / "tools",
        {"ido-7.1": compiler},
        "ido-7.1",
        {},
        version_map,
    )
    policy = SimpleNamespace(
        mips_ld=LINKER,
        mips_readelf=READELF,
        mips_objdump=OBJDUMP,
        cpp=cast(Callable[[str], str], tool)("cpp"),
        cppflags=(),
    )
    source = directory / "alpha.c"
    source.write_text("/* NON_MATCHING: returns one. */\nint alpha(void) { return 1; }\n", encoding="utf-8")
    return project, policy, source


class CompareTests(unittest.TestCase):
    def test_register(self) -> None:
        result = trial_compare.compare_words("us", [0x24020001], [0x24030001])
        self.assertEqual(result.typed["register"], 1)

    def test_immediate(self) -> None:
        result = trial_compare.compare_words("us", [0x24020001], [0x24020002])
        self.assertEqual(result.typed["immediate"], 1)

    def test_relocation_requires_relocation_record(self) -> None:
        target, candidate = [0x3C028000], [0x3C028001]
        self.assertEqual(trial_compare.compare_words("us", target, candidate).typed["immediate"], 1)
        self.assertEqual(trial_compare.compare_words("us", target, candidate, {0: 0xFFFF}).typed["relocation"], 1)

    def test_relocation_does_not_hide_register_change(self) -> None:
        result = trial_compare.compare_words("us", [0x3C028000], [0x3C038000], {0: 0xFFFF})
        self.assertEqual(result.typed["register"], 1)

    def test_order(self) -> None:
        result = trial_compare.compare_words(
            "us", [0x24020001, 0x24030002, 0x03E00008], [0x24030002, 0x24020001, 0x03E00008]
        )
        self.assertEqual(result.typed["order"], 1)
        self.assertFalse(result.typed["missing"] or result.typed["inserted"])

    def test_insertion_keeps_later_words_aligned(self) -> None:
        words = [0x24020001, 0x03E00008, 0]
        result = trial_compare.compare_words("us", words, [0x24030002, *words])
        self.assertEqual(result.identical, len(words))
        self.assertEqual(result.typed["inserted"], 1)
        proof = trial.Trial("alpha", "digest", {"us": result}, [], "next")
        self.assertFalse(proof.identical_everywhere)

    def test_missing(self) -> None:
        result = trial_compare.compare_words("us", [0x24020001, 0x03E00008, 0], [0x03E00008, 0])
        self.assertEqual(result.identical, 2)
        self.assertEqual(result.typed["missing"], 1)

    def test_changed_opcode_and_shift_amount(self) -> None:
        self.assertEqual(trial_compare.compare_words("us", [0x24020001], [0x8C820001]).typed["changed"], 1)
        self.assertEqual(trial_compare.compare_words("us", [0x00021040], [0x00021080]).typed["immediate"], 1)

    def test_branch_displacements_are_never_byte_identity(self) -> None:
        result = trial_compare.compare_words("us", [0x10400001], [0x10400002])
        self.assertEqual(result.identical, 0)
        self.assertEqual(result.typed["immediate"], 1)

    def test_all_types_present_and_empty_proof_is_false(self) -> None:
        result = trial_compare.compare_words("us", [0], [0])
        self.assertEqual(set(result.typed), set(trial_compare.TYPES))
        self.assertFalse(trial.Trial("alpha", "digest", {}, [], "next").identical_everywhere)


class FeatureTests(unittest.TestCase):
    def test_bootstrap_is_idempotent_and_trial_annotation_resolves(self) -> None:
        from typing import get_type_hints

        features.load()
        before = (needs.derivers(), needs.resolvers())
        features.load()
        self.assertEqual(before, (needs.derivers(), needs.resolvers()))
        self.assertEqual(get_type_hints(trial.Trial)["needs"], list[needs.Need])


class TrialTests(unittest.TestCase):
    def setUp(self) -> None:
        (SCRATCH_ROOT).mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=SCRATCH_ROOT)
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.project, self.policy, self.source = fixture(self.directory)
        self.scratch = self.directory / "scratch"
        self.words = [0x24020001, 0x03E00008, 0]

    def compile(self, project: Project, policy: SimpleNamespace, source: Path, version: str, out: Path) -> Path:
        self.assertTrue(out.is_relative_to(self.scratch))
        self.assertTrue(source.is_relative_to(self.scratch))
        self.assertIn("alpha", source.read_text())
        built = assemble(out.parent, "compiled", assembly("alpha", self.words))
        shutil.copyfile(built, out)
        return out

    def attempt(
        self,
        versions: list[str] | None = None,
        compiler: Callable[[Project, SimpleNamespace, Path, str, Path], Path] | None = None,
    ) -> tuple[trial.Trial, str, MagicMock]:
        with (
            patch.object(build, "compile_object", side_effect=compiler or self.compile) as compile_call,
            redirect_stdout(io.StringIO()) as output,
        ):
            result = trial.try_draft(
                self.project, cast(Policy, self.policy), self.source, self.scratch, versions=versions
            )
        return result, output.getvalue(), compile_call

    def snapshot(self) -> dict[str, str | bytes]:
        return {
            str(path.relative_to(self.project.root)): (str(path.readlink()) if path.is_symlink() else path.read_bytes())
            for path in self.project.root.rglob("*")
            if path.is_file() or path.is_symlink()
        }

    def test_identical_trial_preserves_project_and_reports_next_command(self) -> None:
        before = self.snapshot()
        result, output, calls = self.attempt()
        self.assertTrue(result.identical_everywhere)
        self.assertEqual(result.source_sha256, hashlib.sha256(self.source.read_bytes()).hexdigest())
        self.assertEqual(result.compares["us"].identical, 3)
        self.assertIn("match submit", result.next_command)
        self.assertIn("next_command:", output)
        self.assertIn("NON_MATCHING draft", output)
        self.assertEqual(calls.call_count, 1)
        self.assertEqual(before, self.snapshot())
        self.assertTrue(list(self.scratch.rglob("report.txt")))

    def test_committed_nonmatching_draft_is_enabled_for_compile_and_layouts(self) -> None:
        self.source = self.project.src / "alpha.c"
        content = (
            b"#ifdef NON_MATCHING\nstruct Draft { char pad[0x10 - sizeof(u32)]; };\n"
            b"int alpha(void) { return 1; }\n#else\nint matching_only;\n#endif\n"
        )
        self.source.write_bytes(content)
        before = self.snapshot()

        def compiler(project: Project, policy: SimpleNamespace, source: Path, version: str, out: Path) -> Path:
            preprocessed = trial_compile.run_tool([str(policy.cpp), "-P", str(source)], out.parent, "try")
            self.assertIn("int alpha(void)", preprocessed)
            self.assertNotIn("matching_only", preprocessed)
            return self.compile(project, policy, source, version, out)

        result, _, _ = self.attempt(compiler=compiler)
        self.assertTrue(result.identical_everywhere)
        draft = next(item for item in result.needs if isinstance(item, needs.LayoutNeed) and item.struct == "Draft")
        self.assertEqual(cast(list[dict[str, object]], draft.fields)[0]["extent"], (12,))
        self.assertEqual(result.source_sha256, hashlib.sha256(content).hexdigest())
        self.assertEqual(before, self.snapshot())

    def test_generation_can_be_removed_after_snapshot_without_a_lock(self) -> None:
        generation = self.project.build_link("us").resolve()
        run_tool = trial_compile.run_tool
        removed = False

        def remove_after_header(argv: list[str], work: Path, phase: str) -> str:
            nonlocal removed
            result = run_tool(argv, work, phase)
            if "-hW" in argv and not removed:
                self.assertTrue(Path(argv[-1]).is_relative_to(self.scratch))
                shutil.rmtree(generation)
                removed = True
            return result

        with patch.object(trial_link, "run_tool", side_effect=remove_after_header):
            result, _, _ = self.attempt()
        self.assertTrue(removed)
        self.assertTrue(result.identical_everywhere)

    def test_changed_trial_keeps_retry_command(self) -> None:
        self.words[0] = 0x24020002
        result, _, _ = self.attempt(["us"])
        self.assertEqual(result.compares["us"].typed["immediate"], 1)
        self.assertIn("decomp try", result.next_command)
        self.assertIn("--version us", result.next_command)

    def test_adjacent_function_within_split_span_is_compared(self) -> None:
        for changed, outside in ((False, False), (True, False), (False, True)):
            with self.subTest(changed=changed, outside=outside):
                directory = self.directory / f"adjacent-{changed}-{outside}"
                directory.mkdir()
                project, policy, source = fixture(directory, words=[*self.words, 0x03E00008, 0])

                def compiler(
                    p: Project,
                    policy: SimpleNamespace,
                    source: Path,
                    version: str,
                    out: Path,
                    changed: bool = changed,
                    outside: bool = outside,
                ) -> Path:
                    tail = [0x03E00008, 1 if changed else 0]
                    if outside:
                        tail = [0x03E00008, 0, 0]
                    built = assemble(out.parent, "compiled", assembly("alpha", self.words) + assembly("adjacent", tail))
                    shutil.copyfile(built, out)
                    return out

                with patch.object(build, "compile_object", side_effect=compiler), redirect_stdout(io.StringIO()):
                    result = trial.try_draft(project, cast(Policy, policy), source, self.scratch)
                    self.assertEqual(result.identical_everywhere, not changed and not outside)
                    self.assertEqual(result.compares["us"].of, 5)

        def compile_next(p: Project, policy: SimpleNamespace, source: Path, version: str, out: Path) -> Path:
            built = assemble(
                out.parent, "compiled", assembly("alpha", self.words) + assembly("adjacent", [0x03E00008, 0])
            )
            shutil.copyfile(built, out)
            return out

        result, _, _ = self.attempt(compiler=compile_next)
        self.assertTrue(result.identical_everywhere)
        self.assertEqual(result.compares["us"].of, 3)

    def test_explicit_text_residue_participates_in_word_proof(self) -> None:
        (self.directory / "residue").mkdir()
        project, policy, source = fixture(self.directory / "residue", words=[*self.words, 0])

        def compile_residue(p: Project, policy: SimpleNamespace, source: Path, version: str, out: Path) -> Path:
            built = assemble(out.parent, "compiled", assembly("alpha", self.words) + ".word 0\n")
            shutil.copyfile(built, out)
            return out

        with (
            patch.object(build, "compile_object", side_effect=compile_residue),
            redirect_stdout(io.StringIO()),
        ):
            result = trial.try_draft(project, cast(Policy, policy), source, self.scratch)
        self.assertTrue(result.identical_everywhere)
        self.assertEqual(result.compares["us"].of, 4)

    def test_identical_source_without_note_can_be_submitted(self) -> None:
        self.source.write_text("int alpha(void){return 1;}\n", encoding="utf-8")
        result, _output, _ = self.attempt()
        self.assertTrue(result.identical_everywhere)
        self.assertEqual(result.preconditions, [])
        self.assertIn("match submit", result.next_command)

    def test_inline_asm_is_held_before_compile(self) -> None:
        self.source.write_text('void alpha(void){__asm__("nop");}\n', encoding="utf-8")
        with (
            patch.object(build, "compile_object") as compile_call,
            self.assertRaisesRegex(Held, "inline-asm"),
        ):
            trial.try_draft(self.project, cast(Policy, self.policy), self.source, self.scratch)
        compile_call.assert_not_called()

    def test_scratch_inside_project_and_symlink_alias_are_held(self) -> None:
        alias = self.directory / "alias"
        alias.symlink_to(self.project.root, target_is_directory=True)
        for scratch in (self.project.root, self.project.root / "scratch", alias / "scratch"):
            with self.subTest(scratch=scratch), self.assertRaisesRegex(Held, "scratch.*inside project.root"):
                trial.try_draft(self.project, cast(Policy, self.policy), self.source, scratch)

    def test_missing_tool_is_named(self) -> None:
        del self.policy.mips_readelf
        with self.assertRaisesRegex(Held, "policy.mips_readelf"):
            self.attempt()

    def test_split_supplies_missing_symbol_and_boundary_is_required(self) -> None:
        version = self.project.version("us")
        version.symbols.write_text("beta = 0x8000100C;\n", encoding="utf-8")
        self.assertTrue(self.attempt(["us"])[0].identical_everywhere)
        version.symbols.write_text("alpha = 0x80001000;\n", encoding="utf-8")
        version.split.write_text(
            (
                "segments:\n  - name: main\n    start: 0x40\n    vram: 0x80001000\n"
                "    subalign: 4\n    subsegments:\n      - [0x40, asm, alpha]\n"
            ),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(Held, "segment end"):
            self.attempt()

    def test_baserom_short_read_is_held(self) -> None:
        self.project.version("us").baserom.write_bytes(bytes(0x44))
        with self.assertRaisesRegex(Held, "baserom.*bytes.*missing"):
            self.attempt()

    def test_generation_layout_address_must_agree(self) -> None:
        version = self.project.version("us")
        version.symbols.write_text("alpha = 0x80002000;\n", encoding="utf-8")
        version.split.write_text(version.split.read_text().replace("0x80001000", "0x80002000"), encoding="utf-8")
        with self.assertRaisesRegex(Held, "game.elf.*address.*disagrees"):
            self.attempt()

    def test_missing_or_ambiguous_elf_is_held(self) -> None:
        generation = self.project.build_link("us").resolve()
        shutil.copyfile(generation / "game.elf", generation / "other.elf")
        with self.assertRaisesRegex(Held, "exactly one linked.*found 2"):
            self.attempt()
        (generation / "game.elf").unlink()
        (generation / "other.elf").unlink()
        with self.assertRaisesRegex(Held, "exactly one linked.*found 0"):
            self.attempt()

    def test_unplaced_external_is_named(self) -> None:
        def compiler(project: Project, policy: SimpleNamespace, source: Path, version: str, out: Path) -> Path:
            built = assemble(
                out.parent,
                "compiled",
                (
                    ".set noreorder\n.text\n.globl alpha\n.type alpha, @function\nalpha:\n"
                    "lui $v0, %hi(unplaced)\naddiu $v0, $v0, %lo(unplaced)\njr $ra\nnop\n"
                    ".size alpha, .-alpha\n"
                ),
            )
            shutil.copyfile(built, out)
            return out

        with self.assertRaisesRegex(Held, "unplaced: relocation instruction differs"):
            self.attempt(compiler=compiler)

    def test_rodata_placement_is_refused_by_name(self) -> None:
        def compiler(project: Project, policy: SimpleNamespace, source: Path, version: str, out: Path) -> Path:
            built = assemble(out.parent, "compiled", assembly("alpha", self.words) + ".section .rodata\n.word 0x1234\n")
            shutil.copyfile(built, out)
            return out

        with self.assertRaisesRegex(Held, r".rodata.base"):
            self.attempt(compiler=compiler)

    def test_multiple_versions_compile_once_each_with_their_addresses(self) -> None:
        other = self.directory / "multiple"
        other.mkdir()
        self.project, self.policy, self.source = fixture(other, versions=("us", "eu"))
        result, output, calls = self.attempt()
        self.assertEqual(set(result.compares), {"us", "eu"})
        self.assertTrue(result.identical_everywhere)
        self.assertEqual(calls.call_count, 2)
        self.assertIn("80202000", output)

    def test_partial_identical_trial_requests_all_versions_before_submit(self) -> None:
        other = self.directory / "partial"
        other.mkdir()
        self.project, self.policy, self.source = fixture(other, versions=("us", "eu"))
        result, _, calls = self.attempt(["us"])
        self.assertTrue(result.identical_everywhere)
        self.assertEqual(calls.call_count, 1)
        self.assertIn("decomp try", result.next_command)
        self.assertNotIn("--version", result.next_command)

    def test_stripped_symbol_and_label_are_proved_before_link(self) -> None:
        for known in (False, True):
            with self.subTest(known=known):
                other = self.directory / str(known)
                other.mkdir()
                words = [0x3C028000, 0x8C423008, 0x03E00008, 0]
                self.project, self.policy, self.source = fixture(other, words=words)
                version = self.project.version("us")
                original = version.split.read_text()
                end = 0x40 + 4 * (len(words) + 6)
                version.split.write_text(
                    original.replace(
                        f"  - [0x{end:X}]",
                        (
                            "  - name: data\n    type: code\n    start: 0x100\n"
                            "    vram: 0x80003000\n    subalign: 4\n    subsegments:\n"
                            "      - [0x100, data, constants]\n  - [0x110]"
                        ),
                    )
                )
                version.baserom.write_bytes(version.baserom.read_bytes().ljust(0x110, b"\0"))
                if known:
                    with version.symbols.open("a") as stream:
                        stream.write("external = 0x80003004; // type:s32 size:4\n")
                generation = self.project.build_link("us").resolve()
                script = generation / "layout.ld"
                script.write_text(script.read_text().replace("external = 0x80003000;\n", ""))
                subprocess.run(
                    [LINKER, "-T", str(script), "-o", str(generation / "game.elf"), str(generation / "original.o")],
                    check=True,
                    capture_output=True,
                )

                def compiler(project: Project, policy: SimpleNamespace, source: Path, version: str, out: Path) -> Path:
                    built = assemble(
                        out.parent,
                        "compiled",
                        (
                            ".set noreorder\n.text\n.globl alpha\n.type alpha, @function\nalpha:\n"
                            "lui $v0, %hi(external+4)\nlw $v0, %lo(external+4)($v0)\njr $ra\nnop\n"
                            ".size alpha, .-alpha\n"
                        ),
                    )
                    shutil.copyfile(built, out)
                    return out

                result, output, _ = self.attempt(compiler=compiler)
                self.assertTrue(result.identical_everywhere)
                symbol = next(
                    need for need in result.needs if isinstance(need, needs.SymbolNeed) and need.name == "external"
                )
                self.assertEqual((symbol.address, symbol.addend), (0x80003004, 4))
                self.assertTrue(
                    any(isinstance(need, needs.LabelNeed) and need.name == "external" for need in result.needs)
                )
                self.assertIn("external", output)
                self.assertEqual(
                    trial_layout.symbol_values(version.symbols).get("external"), 0x80003004 if known else None
                )

    def test_constant_artifacts_prove_rom_bytes_owners_and_link_placement(self) -> None:
        from dataclasses import replace

        from unbake.layout import rodata

        for ident, section in [("ido-7.1", ".rodata"), ("gcc-2.8.1-sn64", ".rdata")]:
            with self.subTest(compiler=ident):
                directory = self.directory / ident
                directory.mkdir()
                words = [0x3C028000, 0x8C421028, 0x03E00008, 0]
                project, _policy, source = fixture(directory, words=words)
                compiler = replace(project.compiler_for(source), id=ident)
                project = replace(project, compilers={ident: compiler}, default_compiler=ident)
                version = project.version("us")
                version.split.write_text(
                    version.split.read_text().replace("  - [0x68]", "      - [0x68, rodata, constants]\n  - [0x6C]")
                )
                version.baserom.write_bytes(version.baserom.read_bytes() + bytes.fromhex("3f800000"))
                work = directory / "work"
                work.mkdir()
                object_path = assemble(
                    work,
                    "compiled",
                    (
                        ".set noreorder\n.text\n.globl alpha\n.type alpha, @function\nalpha:\n"
                        "lui $v0, %hi(pool)\nlw $v0, %lo(pool)($v0)\njr $ra\nnop\n"
                        ".size alpha, .-alpha\n"
                    )
                    + f".section {section}\npool:\n.word 0x3f800000\n",
                )
                unit = trial_link.inspect(object_path, READELF, work)
                layout = trial_link.inspect(project.build_link("us").resolve() / "game.elf", READELF, work)
                span = trial_layout.function_span(version, "alpha", trial_layout.symbol_values(version.symbols))
                assert span is not None
                artifact: trial_artifacts.Artifact = dict(
                    unit=unit, layout=layout, target_words=words, span=span, version=version, work=work
                )
                constants = trial_artifacts.rodata_object(project, source, "alpha", artifact, {})
                self.assertEqual(constants.owners[(section, 0)], ("alpha",))
                pending = rodata.needs(constants, "us")
                self.assertEqual(pending[0].address, 0x80001028)
                linked, _, _ = trial_link.link(unit, layout, "alpha", span, {}, pending, LINKER, READELF, work)
                self.assertEqual(trial_compare.words(linked), words)
                version.baserom.write_bytes(version.baserom.read_bytes()[:-4] + bytes(4))
                with self.assertRaisesRegex(Held, r"bytes: disagree"):
                    rodata.needs(constants, "us")


if __name__ == "__main__":
    unittest.main()
