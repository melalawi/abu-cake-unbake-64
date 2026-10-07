"""Real exit-zero IDO #error diagnostics must not become native comparisons or cached objects."""

import hashlib
import json
import os
import subprocess
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from tests.kit import TESTS
from tests.project_fixture import ProjectCase
from unbake import cache, config, process, runner
from unbake.compilers import drivers, registry
from unbake.compilers.families import family_for
from unbake.config import Held
from unbake.layout import split
from unbake.objects.elf import Object
from unbake.work import compare

FIXTURE = (TESTS / "compilers/test_active_error.py").parent / "fixtures/active_error"
PROOF = json.loads((FIXTURE / "results.json").read_text())
FUNCTION = "func_8028FDF8_de"
SOURCE_SHA = "f82b2c721a0909fa6323feb3441b0656f029d7983b32c7e6e744c76330aa8fb9"


class ActiveErrorTests(ProjectCase):
    native = False

    def setUp(self):
        super().setUp()
        if self.native and not os.environ.get("UNBAKE_NATIVE_COMPILER_ROOT"):
            self.skipTest("pinned native compiler root not configured; real streams/objects retained")
        # Give the small project's row the actual source identity; no game or map solve.
        root = self.project.root
        for version in self.versions:
            for path in (self.project.version(version).split, self.project.version(version).symbols):
                path.write_text(path.read_text().replace("alpha", FUNCTION))
            asm = root / "extract" / version / "asm/alpha.s"
            (asm.parent / (FUNCTION + ".s")).write_text(asm.read_text().replace("alpha", FUNCTION))
        layout = root / "layout.toml"
        layout.write_text(layout.read_text().replace("alpha", FUNCTION))
        self.project = config.load(root)
        (self.project.include[0] / "types.h").write_bytes((FIXTURE / "types.h").read_bytes())
        self.source = self.project.src / (FUNCTION + ".c")
        self.calls = []
        self.actual_run = subprocess.run

    def selected(self, release):
        spec = registry.specification(release)
        compiler = replace(self.project.compilers[self.project.default_compiler], id=release, cflags=spec.cflags)
        if self.native:
            compiler = replace(compiler, cc=Path(os.environ["UNBAKE_NATIVE_COMPILER_ROOT"]) / release / spec.cc)
        self.project = replace(self.project, compilers={release: compiler}, default_compiler=release)
        self.release = release

    def output(self, argv, **kwargs):
        if self.native:
            result = self.actual_run(argv, **kwargs)
        elif "-E" in argv:
            source = Path(argv[-1])
            source = source if source.is_absolute() else Path(kwargs["cwd"]) / source
            digest = hashlib.sha256(source.read_bytes()).hexdigest()
            version = "eu" if "-DVERSION_EU" in argv else "us"
            key = next(
                k
                for k, record in PROOF.items()
                if k.startswith(self.release + "/")
                and (k.endswith("-" + version) or "/conditional-" not in k)
                and record["source_sha256"] == digest
            )
            saved = FIXTURE / key
            stdout = (saved / "stdout").read_text().replace('"source.c"', '"' + str(argv[-1]) + '"')
            stderr = (saved / "stderr").read_text().replace("source.c:", str(argv[-1]) + ":")
            result = subprocess.CompletedProcess(argv, PROOF[key]["exit"], stdout, stderr)
        else:
            saved = FIXTURE / self.release / "valid-us"
            Path(kwargs["cwd"], argv[argv.index("-o") + 1]).write_bytes((saved / "source.o").read_bytes())
            result = subprocess.CompletedProcess(argv, 0, "", "")
        self.calls.append((tuple(argv), result.returncode, result.stdout, result.stderr))
        return result

    def body(self, variant):
        self.source.write_bytes((FIXTURE / (variant + ".c")).read_bytes())

    def compile(self, version="us"):
        with runner.compile_unit(self.project, self.host, self.source, version, unit=FUNCTION) as obj:
            parsed = Object(obj)
            return parsed.content(parsed.section(".text"))

    def test_active_real_body_refuses_warm_identical_output_before_cache_or_compile(self):
        for release in ("ido-7.1", "ido-5.3"):
            with self.subTest(release=release):
                self.selected(release)
                self.calls.clear()
                self.body("valid")
                with patch.object(process.subprocess, "run", side_effect=self.output):
                    before = self.compile()
                    warm_output = self.calls[-2][2]
                    self.body("active")
                    self.assertEqual(hashlib.sha256(self.source.read_bytes()).hexdigest(), SOURCE_SHA)
                    with (
                        patch.object(
                            cache.Cache, "produce", side_effect=AssertionError("invalid source reached CAS")
                        ) as cas,
                        self.assertRaises(Held) as raised,
                    ):
                        self.compile()
                    self.assertEqual(cas.call_count, 0)
                    fault = raised.exception.fault
                    self.assertEqual(fault.cause.key, "compile.preprocessor_directive")
                    native = process.native_results(fault)
                    self.assertEqual(len(native), 1)
                    self.assertEqual((native[0].exit, native[0].stdout), (0, warm_output))
                    self.assertIn("#error Intended result ABI", native[0].stderr)
                    self.assertEqual(native[0].args, self.calls[-1][0])
                    self.assertEqual(native[0].context["version"], "us")
                    self.assertEqual(fault.cause.location.line, 4)
                    self.assertEqual(self.source.read_bytes(), (FIXTURE / "source.c").read_bytes())
                    self.body("valid")
                    self.assertEqual(self.compile(), before)
                self.assertEqual(sum("-E" in call[0] for call in self.calls), 3)
                self.assertEqual(sum("-c" in call[0] for call in self.calls), 1)

    def test_public_compare_retains_first_cause_and_never_links_active_body(self):
        self.selected("ido-7.1")
        self.body("active")
        with (
            patch.object(process.subprocess, "run", side_effect=self.output),
            patch.object(cache.Cache, "produce", autospec=True, side_effect=cache.Cache.produce) as cas,
            patch.object(split, "words", wraps=split.words) as reads,
            patch.object(runner, "link_function", return_value=(bytes(48), [])) as link,
        ):
            measured = compare.measure(self.project, self.host, self.source)
        self.assertFalse(any(result.available for result in measured.compares.values()))
        self.assertEqual((cas.call_count, link.call_count, reads.call_count, len(self.calls)), (0, 0, 2, 2))
        self.assertEqual(measured.source_sha256, SOURCE_SHA)
        self.assertEqual(measured.best_percent, None)
        for version in self.versions:
            fault = process.Fault.read(measured.document()["versions"][version]["fault"])
            self.assertEqual(fault.cause.key, "compile.preprocessor_directive")
            native = process.native_results(fault)
            self.assertEqual(len(native), 1)
            self.assertEqual(native[0].exit, 0)
            self.assertIn("#error Intended result ABI", native[0].stderr)
            self.assertEqual(native[0].context["version"], version)

    def test_inactive_and_version_conditional_directives_follow_actual_preprocessor(self):
        for release in ("ido-7.1", "ido-5.3"):
            with self.subTest(release=release):
                self.selected(release)
                self.calls.clear()
                with patch.object(process.subprocess, "run", side_effect=self.output):
                    self.body("inactive")
                    self.assertTrue(self.compile())
                    self.body("conditional")
                    with self.assertRaises(Held) as raised:
                        self.compile("us")
                    self.assertEqual(raised.exception.fault.cause.key, "compile.preprocessor_directive")
                    self.assertTrue(self.compile("eu"))
                self.assertEqual(sum("-E" in call[0] for call in self.calls), 3)
                self.assertEqual(sum("-c" in call[0] for call in self.calls), 1)
                # Inactive US and conditional EU have the same effective text and object identity.
                self.assertEqual(self.calls[0][2], self.calls[-1][2])
                self.assertEqual(sum(bool(call[3]) for call in self.calls), 1)

    def test_family_boundary_keeps_complete_success_stream_and_real_failure_transport(self):
        self.selected("ido-7.1")
        self.body("active")
        with patch.object(process.subprocess, "run", side_effect=self.output), self.assertRaises(Held):
            runner.preprocess(self.project, self.host, self.source, "us", unit=FUNCTION)
        # GNU's host cpp already fails active directives. Nonzero exits stay native faults.
        argv = ["cpp", "-I" + str(self.project.include[0]), str(self.source)]
        result = self.actual_run(argv, capture_output=True, text=True, check=False)
        self.assertNotEqual(result.returncode, 0)
        for ident in ("gcc-2.7.2-kmc", "gcc-2.8.1-sn64"):
            spec = registry.specification(ident)
            compiler = replace(self.project.compiler_for(FUNCTION), id=ident, kind=spec.kind)
            project = replace(self.project, compilers={ident: compiler}, default_compiler=ident)
            with patch.object(process.subprocess, "run", return_value=result), self.assertRaises(Held) as raised:
                drivers.run_preprocess(project, argv, "compile", unit=FUNCTION)
            self.assertEqual(process.native_results(raised.exception.fault)[0].stderr, result.stderr)
            success = process.NativeResult(
                tuple(argv),
                str(project.root),
                0,
                None,
                "expanded\n",
                "",
                "success",
                None,
                "utf-8",
                "surrogateescape",
                {},
            )
            self.assertEqual(family_for(ident).preprocessed(success), success.stdout)


class NativeActiveErrorTests(ActiveErrorTests):
    native = True
