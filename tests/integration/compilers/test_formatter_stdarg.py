"""Actual formatter control-flow/codegen survives supported public va_arg normalization."""

import os
import shutil
import subprocess
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from tests.kit import TESTS
from tests.project_fixture import ProjectCase
from unbake import land
from unbake.compilers import headers, registry
from unbake.config import Compiler
from unbake.objects.elf import Object

FIXTURES = (TESTS / "compilers/test_formatter_stdarg.py").parent / "fixtures" / "formatter_stdarg"


def relocation_view(obj, section):
    return [
        (at, kind, symbol["name"], symbol["value"], symbol["section"])
        for at, kind, symbol in obj.relocations(obj.section(section))
    ]


class FormatterStdargTests(ProjectCase):
    versions = ("us",)

    def selected(self, ident):
        spec = registry.specification(ident)
        compiler = Compiler(
            ident,
            spec.kind,
            self.project.tools / ident / spec.cc,
            Path(spec.as_),
            spec.cflags,
            self.project.tools / "pins",
        )
        project = replace(self.project, compilers={ident: compiler}, default_compiler=ident)
        root = project.include[0]
        (root / "common").mkdir()
        (root / "types.h").write_bytes((FIXTURES / "types.h").read_bytes())
        (root / "common/unused.h").write_bytes((FIXTURES / "unused.h").read_bytes())
        return project, spec

    def compile_pair(self, ident):
        from unbake.compilers.families import family_for

        compiler_root = os.environ.get("UNBAKE_NATIVE_COMPILER_ROOT")
        normalizer = os.environ.get("UNBAKE_NATIVE_N64LINK")
        assembler = shutil.which("mips-linux-gnu-as")
        cpp = shutil.which("cpp")
        if not compiler_root or not normalizer or not assembler or not cpp:
            self.skipTest("recorded real mismatch retained; pinned native toolchain not configured")
        project, spec = self.selected(ident)
        compiler = Path(compiler_root) / ident / spec.cc
        registry.verify(Path(compiler_root) / ident, spec)
        from unbake.typemap import header_names

        with patch.object(header_names, "alias_types", wraps=header_names.alias_types) as parsed:
            headers.run(project)
        self.assertEqual(parsed.call_count, 1)
        calls = []
        objects = []
        for stem in ("before", "after"):
            source = self.root / (stem + ".c")
            source.write_bytes((FIXTURES / (stem + ".c")).read_bytes())
            argv = [
                cpp,
                "-P",
                "-undef",
                "-nostdinc",
                "-D__GNUC__=2",
                *family_for(ident).public_defines(),
                "-I" + str(project.include[0]),
                source.name,
            ]
            result = subprocess.run(argv, cwd=self.root, capture_output=True, text=True, check=True)
            expanded = self.root / (stem + ".i")
            expanded.write_text(result.stdout)
            calls.append("cpp")
            if stem == "after":
                land._fuzzy_calls("func_802BD974_de", result.stdout, intrinsics=family_for(ident).source_intrinsics())
            asm = self.root / (stem + ".s")
            subprocess.run(
                [str(compiler), "-quiet", *spec.cflags, expanded.name, "-o", asm.name],
                cwd=self.root,
                capture_output=True,
                text=True,
                check=True,
            )
            calls.append("compile")
            obj = self.root / (stem + ".o")
            subprocess.run(
                [
                    normalizer,
                    "asn64",
                    "--as",
                    assembler,
                    "-march=vr4300",
                    "-mabi=32",
                    "-EB",
                    "-G0",
                    "--no-pad-sections",
                    asm.name,
                    "-o",
                    obj.name,
                ],
                cwd=self.root,
                capture_output=True,
                text=True,
                check=True,
            )
            calls.append("assemble")
            objects.append(Object(obj))
        self.assertEqual(calls, ["cpp", "compile", "assemble"] * 2)
        return objects

    def assert_exact(self, objects):
        old, new = objects
        self.assertEqual(old.content(old.section(".text")), new.content(new.section(".text")))
        self.assertEqual(relocation_view(old, ".text"), relocation_view(new, ".text"))
        for section in old.names:
            if section in (".rodata", ".rdata"):
                self.assertEqual(old.content(old.section(section)), new.content(new.section(section)))
                self.assertEqual(relocation_view(old, section), relocation_view(new, section))

    def test_real_sn64_formatter_is_whole_function_byte_exact(self):
        self.assert_exact(self.compile_pair("gcc-2.8.1-sn64"))

    def test_real_kmc_formatter_is_whole_function_byte_exact(self):
        self.assert_exact(self.compile_pair("gcc-2.7.2-kmc"))

    def test_recorded_main_regression_is_whole_controlflow_not_an_abi_size_change(self):
        before, after = Object(FIXTURES / "before.o"), Object(FIXTURES / "after.o")
        self.assertEqual(len(before.content(before.section(".text"))), 1132)
        self.assertEqual(len(after.content(after.section(".text"))), 1128)
        self.assertNotEqual(before.content(before.section(".text")), after.content(after.section(".text")))
