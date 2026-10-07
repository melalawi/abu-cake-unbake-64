"""Public provisioning and real native stdarg ABI/cursor/once-only evidence."""

import hashlib
import json
import shutil
import struct
import subprocess
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from tests.kit import TESTS
from tests.project_fixture import ProjectCase
from unbake import steps
from unbake.compilers import registry
from unbake.config import Compiler, Held
from unbake.objects.elf import Object

FIXTURES = (TESTS / "compilers/test_public_stdarg.py").parent / "fixtures" / "stdarg"
PROOF = json.loads((FIXTURES / "native-results.json").read_text())


class PublicStdargTests(ProjectCase):
    versions = ("us",)

    def setUp(self):
        super().setUp()
        compilers = {}
        for ident, spec in registry.registry().items():
            compilers[ident] = Compiler(
                ident,
                spec.kind,
                self.project.tools / ident / spec.cc,
                Path(spec.as_),
                spec.cflags,
                self.project.tools / "compilers.sha256",
            )
        self.project = replace(self.project, compilers=compilers, default_compiler="gcc-2.8.1-sn64")

    def test_public_recompute_provisions_missing_provider_without_map_types_or_native_work(self):
        public = self.project.include[0] / "stdarg.h"
        self.assertFalse(public.exists())
        with (
            patch("unbake.process.run_native", side_effect=AssertionError("no native work")),
            patch("unbake.typemap.solver.solve", side_effect=AssertionError("no type solve")),
            patch("unbake.typemap.mapping.refresh_map", side_effect=AssertionError("no map solve")),
        ):
            ran = steps.recompute(self.project, self.host, ("compiler-headers",))
        self.assertEqual([r.step for r in ran], ["compiler-headers"])
        self.assertEqual(public.read_bytes(), (FIXTURES / "stdarg.h").read_bytes())
        from unbake.compilers import headers

        before = public.stat().st_mtime_ns
        self.assertEqual(headers.run(self.project), [])
        self.assertEqual(public.stat().st_mtime_ns, before)
        cpp = shutil.which("cpp")
        if cpp:
            source = self.root / "use.c"
            source.write_text("#include <stdarg.h>\nint f(va_list *p) { return va_arg(*p,int); }\n")
            command = [
                cpp,
                "-P",
                "-undef",
                "-nostdinc",
                "-I" + str(self.project.include[0]),
                "-D__UNBAKE_STDARG_GCC=1",
                str(source),
            ]
            result = subprocess.run(command, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("__unbake_stdarg_take", result.stdout)

    def test_family_selectors_and_analysis_contract_are_explicit(self):
        from unbake.compilers import drivers, headers
        from unbake.compilers.families import family_for

        headers.run(self.project)
        for ident in self.project.compilers:
            family = family_for(ident)
            selected = replace(self.project, default_compiler=ident)
            stage = drivers.steps(selected, "us", "alpha", "src/alpha.c", drivers.Tools("cpp", "as", "n64link"))
            for define in family.public_defines():
                self.assertIn(define, stage.preprocess)
            self.assertEqual(len(family.public_headers()), 1)

    def test_existing_compatible_type_provider_is_imported_without_duplicate_typedef(self):
        from unbake.compilers import headers

        owner = self.project.include[0] / "owned.h"
        owner.write_text("#ifndef OWNED\n#define OWNED\ntypedef char * va_list;\n#endif\n")
        before = owner.read_bytes()
        headers.run(self.project)
        public = (self.project.include[0] / "stdarg.h").read_text()
        self.assertIn("#include <owned.h>", public)
        self.assertNotIn("typedef char *va_list;", public)
        self.assertEqual(owner.read_bytes(), before)

    def test_incompatible_or_ambiguous_alias_is_refused_without_header_writes(self):
        from unbake.compilers import headers

        root = self.project.include[0]
        (root / "one.h").write_text("typedef void *va_list;\n")
        with self.assertRaisesRegex(Held, "incompatible existing type"):
            headers.run(self.project)
        self.assertFalse((root / "stdarg.h").exists())
        (root / "one.h").write_text("typedef char *va_list;\n")
        (root / "two.h").write_text("typedef char *va_list;\n")
        with self.assertRaisesRegex(Held, "multiple existing providers"):
            headers.run(self.project)
        self.assertFalse((root / "stdarg.h").exists())

    def test_existing_sdk_or_human_public_provider_is_preserved(self):
        from unbake.compilers import headers

        public = self.project.include[0] / "stdarg.h"
        content = b"/* owned SDK provider */\ntypedef char *va_list;\n"
        public.write_bytes(content)
        self.assertEqual(headers.run(self.project), [])
        self.assertEqual(public.read_bytes(), content)

    def test_real_native_sizes_and_fifteen_gnu_reads_are_byte_exact(self):
        from unbake.compilers import headers

        headers.run(self.project)
        self.assertEqual((self.project.include[0] / "stdarg.h").read_bytes(), (FIXTURES / "stdarg.h").read_bytes())
        for ident, proof in PROOF.items():
            root = FIXTURES / ident
            self.assertEqual(hashlib.sha256((root / "caller.o").read_bytes()).hexdigest(), proof["object_sha256"])
            obj = Object(root / "caller.o")
            symbol = next(s for table in obj.symbols.values() for s in table if s["name"] == "target_sizes")
            data = obj.content(symbol["section"])[symbol["value"] : symbol["value"] + 24]
            self.assertEqual(struct.unpack(">6I", data), (4, 4, 8, 8, 4, 4))
            if (root / "raw.o").exists():
                raw, public = Object(root / "raw.o"), Object(root / "public.o")
                self.assertEqual(raw.content(raw.section(".text")), public.content(public.section(".text")))
        self.assertEqual(len(json.loads((FIXTURES / "requests.json").read_text())), 15)

    def test_native_expansions_are_admitted_using_only_proved_family_intrinsics(self):
        from unbake import land
        from unbake.compilers.families import family_for

        for ident in PROOF:
            family = family_for(ident)
            source = (FIXTURES / ident / "caller.i").read_text()
            land._fuzzy_calls("collect", source, intrinsics=family.source_intrinsics())
            with self.assertRaisesRegex(Held, "undeclared __builtin_va_start"):
                land._fuzzy_calls(
                    "bad", "int bad(void) { return __builtin_va_start(); }", intrinsics=family.source_intrinsics()
                )

    def test_manual_varargs_pointer_arithmetic_still_triggers_the_existing_guard(self):
        from unbake.decomp import checks

        source = (FIXTURES / "raw-printf.c").read_text()
        findings = checks.unmarked(source)
        self.assertTrue(any(row.rule == "raw-offset" for row in findings))

    def test_actual_native_callers_preserve_alignment_fp_prefix_copy_and_once_only_evaluation(self):
        emulator = shutil.which("qemu-mips")
        if not emulator:
            self.skipTest("native recorded proof retained; qemu-mips unavailable on this host")
        for ident, proof in PROOF.items():
            with self.subTest(compiler=ident):
                executable = FIXTURES / ident / "caller.elf"
                self.assertEqual(hashlib.sha256(executable.read_bytes()).hexdigest(), proof["elf_sha256"])
                result = subprocess.run(
                    [emulator, "-cpu", "24Kf", str(executable)], capture_output=True, text=True, timeout=10
                )
                self.assertEqual(result.returncode, 0, result.stderr)
