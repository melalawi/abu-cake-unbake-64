"""Public projected C preserves aliases used by dependent fields and inline locals."""

import os
import re
import subprocess
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from tests.kit import TempCase
from tests.preprocessor import expand
from unbake import runner
from unbake.config import Held
from unbake.fold import declarations, provider_reuse
from unbake.layout import split
from unbake.work import compare

FIXTURE = Path(__file__).parent / "fixtures/dependent_alias"


class DependentAliasTests(TempCase):
    def setUp(self):
        super().setUp()
        from tests.fold.test_provider_semantics import ProviderSemanticTests

        self.case = ProviderSemanticTests()
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        self.project, self.host, self.view = self.case.project, self.case.host, self.case.view
        self.source, self.private = self.case.source, self.case.private
        # The actual private include view shadows this public owner path.
        relative = self.case.owner.relative_to(self.project.include[-1])
        self.shadow = self.view.include[0] / relative
        self.shadow.parent.mkdir(parents=True)
        self.shadow.write_bytes((FIXTURE / "owner.h").read_bytes())

    def expanded(self, view, file):
        return expand(file.read_text(), roots=view.include, cwd=file.parent)

    def assert_aliases(self, text):
        for name in ("Header_func_8025B5F0_de", "Owner_func_8025B5F0_de"):
            self.assertEqual(len(re.findall(r"typedef\s+struct\s+" + name + r"\s+" + name + r"\s*;", text)), 1)
            self.assertEqual(len(re.findall(r"struct\s+" + name + r"\s*\{", text)), 1)
        self.assertIn("Owner_func_8025B5F0_de *owner = slot->owner", text)
        self.assertEqual(text.count("struct SlotCC {"), 1)

    def test_public_compare_keeps_owner_aliases_in_the_final_staged_context(self):
        observed = []

        @contextmanager
        def compile_unit(view, host, file, version, **kwargs):
            self.assert_aliases(self.expanded(view, file))
            observed.append((version, view.include[0]))
            yield self.root / "unit.o"

        with (
            patch.object(runner, "compile_unit", side_effect=compile_unit) as compile_calls,
            patch.object(
                runner,
                "link_function",
                side_effect=lambda p, h, o, v, row, f: (
                    split.words(p, row),
                    [],
                ),
            ) as links,
        ):
            measured = compare.measure(self.project, self.host, self.source)
        self.assertTrue(measured.exact)
        self.assertEqual((compile_calls.call_count, links.call_count), (5, 5))
        self.assertEqual(len({path for _, path in observed}), 1)
        self.assertFalse(observed[0][1].exists())
        self.assertEqual(self.private.read_bytes(), (FIXTURE / "private.h").read_bytes())
        self.assertEqual(self.source.read_bytes(), (FIXTURE / self.source.name).read_bytes())

    def test_public_fold_keeps_the_dependency_owner_and_has_no_back_import(self):
        observed = []

        def folded(project, host, headers, function, text, versions, **kwargs):
            self.case.assert_layouts(headers)
            self.assertIn("typedef struct Header_func_8025B5F0_de Header_func_8025B5F0_de;", headers.texts[self.shadow])
            observed.append(headers)
            return declarations.Folded(function, text, [], {})

        with (
            patch.object(declarations, "fold_source", side_effect=folded) as fold,
            patch("unbake.layout.structs_fold._prove_includers") as proof,
        ):
            edits = declarations.folded_edits(
                self.view, self.host, self.source.stem, self.source.read_text(), self.case.versions
            )
        self.assertEqual((fold.call_count, proof.call_count), (1, 1))
        self.assertEqual(len(edits), 2)
        self.assertEqual({edit.path for edit in edits}, {self.private, self.project.src / self.source.name})
        self.assertNotIn("shared/", self.shadow.read_text())
        self.assertEqual(self.shadow.read_bytes(), (FIXTURE / "owner.h").read_bytes())
        self.assertEqual(len(observed), 1)

    def test_work_catalogues_each_of_four_effective_headers_once_and_writes_one_consumer(self):
        from unbake import cdecl
        from unbake.project.headers import include_headers

        contents = {path: path.read_text() for path, _ in include_headers(self.view)}
        with (
            patch.object(provider_reuse, "_catalog", wraps=provider_reuse._catalog) as catalog,
            patch.object(cdecl, "parse", wraps=cdecl.parse) as parses,
            patch.object(Path, "read_text", side_effect=AssertionError("no payload reread")),
            patch.object(Path, "write_text", side_effect=AssertionError("no planner writes")),
            patch("subprocess.run", side_effect=AssertionError("no planner native work")) as native,
        ):
            edits = provider_reuse.plan(self.view, contents, self.case.versions)
        self.assertEqual((catalog.call_count, parses.call_count), (4, 14))
        self.assertEqual([edit.path for edit in edits], [self.private])
        native.assert_not_called()

    def test_required_alias_conflict_still_holds_before_any_compiler(self):
        original = self.shadow.read_text()
        self.shadow.write_text(original.replace("s16 local;", "s32 local;"))
        with patch.object(runner, "compile_unit") as native, self.assertRaises(Held) as caught:
            compare.measure(self.project, self.host, self.source)
        self.assertEqual(caught.exception.key, "headers.declaration.duplicate-shared-provider")
        native.assert_not_called()
        self.assertIn("owner.h", caught.exception.reason)

    def test_real_native_C_accepts_public_projection_including_inline_body(self):
        compiler = os.environ.get("UNBAKE_NATIVE_CC1")
        if not compiler:
            self.skipTest("set UNBAKE_NATIVE_CC1 to the pinned read-only MIPS cc1 for native acceptance")
        calls = []

        @contextmanager
        def compile_unit(view, host, file, version, **kwargs):
            if not calls:
                preprocessed = subprocess.run(
                    [
                        "/usr/bin/cpp",
                        "-E",
                        "-DVERSION_DE",
                        "-UNON_MATCHING",
                        *("-I" + str(root) for root in view.include),
                        str(file),
                    ],
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(preprocessed.returncode, 0, preprocessed.stderr)
                expanded = preprocessed.stdout
                source = self.root / "projected.i"
                source.write_text(expanded)
                ran = subprocess.run(
                    [
                        compiler,
                        "-quiet",
                        "-G0",
                        "-mips3",
                        "-O2",
                        "-mgas",
                        "-meb",
                        "-mcpu=VR4300",
                        "-mhard-float",
                        "-mgp32",
                        "-mfp64",
                        "-mno-fix4300",
                        str(source),
                        "-o",
                        str(self.root / "unit.s"),
                    ],
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(ran.returncode, 0, ran.stderr)
                self.assert_aliases(expanded)
            calls.append(version)
            yield self.root / "unit.o"

        with (
            patch.object(subprocess, "run", wraps=subprocess.run) as processes,
            patch.object(runner, "compile_unit", side_effect=compile_unit) as compile_calls,
            patch.object(runner, "link_function", side_effect=lambda p, h, o, v, row, f: (split.words(p, row), [])),
        ):
            measured = compare.measure(self.project, self.host, self.source)
        self.assertTrue(measured.exact)
        self.assertEqual(compile_calls.call_count, 5)
        self.assertEqual(processes.call_count, 2)
        self.assertEqual(calls, list(self.case.versions))
