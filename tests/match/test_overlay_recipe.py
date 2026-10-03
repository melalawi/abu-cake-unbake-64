"""Trial and submit share ordered includes at every source preprocessing boundary."""

import argparse
import json
import subprocess
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from tests.match.support import MatchFixture
from unbake.decomp import explain, work
from unbake.layout.header_context import Headers
from unbake.match import batch_fold, declarations, source_views
from unbake.project import build, makefile
from unbake.project_tools import compile as driver
from unbake.typemap import declarations as typed


class OverlayRecipeTests(MatchFixture):
    def proposal(self):
        root = self.sources / "headers"
        root.mkdir()
        (root / "proposal.h").write_text("#define PROPOSAL 1\n")
        source = self.sources / "alpha.c"
        source.write_text('#include "proposal.h"\nint alpha(void) { return PROPOSAL; }\n')
        return work.overlay_source(self.project, source, self.sources / "authored", root)

    def test_partial_explicit_overlay_keeps_current_headers_and_wins(self):
        root = self.sources / "headers"
        root.mkdir()
        (root / "types.h").write_text("typedef unsigned int word;\n")
        (self.project.include[0] / "published.h").write_text("typedef int Published;\n")
        source = self.sources / "alpha.c"
        source.write_text("int alpha(void) { return 1; }\n")
        staged = work.overlay_source(self.project, source, self.sources / "authored", root)
        configured = work.compilation_project(self.project, staged)
        self.assertEqual(configured.include[1], self.project.include[0])
        self.assertEqual((configured.include[0] / "published.h").read_text(), "typedef int Published;\n")
        self.assertEqual((configured.include[0] / "types.h").read_text(), "typedef unsigned int word;\n")
        self.assertEqual(set(work.overlay_data(self.project, staged)["edits"]), {"include/types.h"})
        self.assertEqual((self.project.include[0] / "types.h").read_text(), "typedef int word;\n")

    def test_historical_sparse_overlay_falls_through_without_deletion(self):
        source = self.proposal()
        path = source.parent / "overlay/include/types.h"
        path.unlink()
        configured = work.compilation_project(self.project, source)
        self.assertEqual(configured.include[1], self.project.include[0])
        self.assertNotIn("include/types.h", work.overlay_data(self.project, source)["edits"])
        (self.project.include[0] / "new.h").write_text("typedef int New;\n")
        work.overlay_data(self.project, source)

    def ordered(self, command, configured):
        overlay = "-I" + str(configured.include[0])
        project = "-Iinclude"
        self.assertIn(overlay, command)
        self.assertIn(project, command)
        self.assertLess(command.index(overlay), command.index(project))

    def test_object_inspection_preprocess_orders_overlay_for_both_compilers(self):
        source = self.proposal()
        configured = work.compilation_project(self.project, source)
        for kind in ("sn64", "gcc"):
            compiler = replace(configured.compilers[configured.default_compiler], kind=kind, cflags=("-Iinclude",))
            selected = replace(configured, compilers={compiler.id: compiler})
            with patch.object(build, "_run", return_value=b"expanded") as run:
                build.preprocess_object(selected, self.policy, source, "us")
            self.ordered(run.call_args.args[0], configured)
            self.ordered(list(makefile.flags(selected, "us", source)), configured)

    def test_divergence_debug_preprocessor_orders_overlay_for_both_compilers(self):
        source = self.proposal()
        configured = work.compilation_project(self.project, source)
        for kind in ("sn64", "gcc"):
            compiler = replace(configured.compilers[configured.default_compiler], kind=kind, cflags=("-Iinclude",))
            selected = replace(configured, compilers={compiler.id: compiler})
            with patch("unbake.decomp.trial_compile.run_tool", return_value="expanded") as run:
                explain.gcc_input(selected, self.policy, source, "us", self.sources, preserve_lines=True)
            command = run.call_args.args[0]
            overlay = "-I" + str(configured.include[0])
            project = "-I" + str(self.project.include[0])
            self.assertIn(overlay, command)
            self.assertIn(project, command)
            self.assertLess(command.index(overlay), command.index(project))

    def test_generated_compile_recipe_orders_dependency_and_source_preprocessing(self):
        source = self.proposal()
        configured = work.compilation_project(self.project, source)
        for kind in ("sn64", "gcc"):
            compiler = replace(configured.compilers[configured.default_compiler], kind=kind, cflags=("-Iinclude",))
            selected = replace(configured, compilers={compiler.id: compiler})
            data = makefile.description(selected)
            args = argparse.Namespace(
                source=source,
                output=self.sources / (kind + ".o"),
                version="us",
                kind="cc",
                unit="src/alpha.c",
                non_matching="1",
                depfile=self.sources / (kind + ".d"),
                dep_target=None,
                recipe=self.project.tools / "build.json",
            )
            commands = []

            def run(command, commands=commands):
                commands.append(command)
                if "-M" in command:
                    return b"alpha.o: alpha.c\n"
                raise RuntimeError("captured source preprocessing")

            with (
                patch.object(driver, "run", side_effect=run),
                patch.object(driver, "resolve_tool", side_effect=lambda value: value),
                self.assertRaisesRegex(RuntimeError, "captured"),
            ):
                driver.compile_object(args, data)
            self.assertEqual(len(commands), 1 if kind == "sn64" else 2)
            for command in commands:
                self.ordered(command, configured)

    def test_declaration_preprocessor_sees_fold_context_before_project(self):
        source = self.proposal()
        headers = Headers.read(self.project)
        proposal = source.parent / "overlay/include/proposal.h"
        headers.texts[self.project.include[0] / "proposal.h"] = proposal.read_text()
        text = '#include "proposal.h"\n#if PROPOSAL\nint alpha(void) {return 1;}\n#endif\n'
        commands = []

        def run(command, **kwargs):
            commands.append(command)
            include = Path(next(flag[2:] for flag in command if flag.startswith("-I")))
            self.assertEqual((include / "proposal.h").read_text(), proposal.read_text())
            self.assertLess(command.index("-I" + str(include)), command.index("-I" + str(self.project.include[0])))
            return subprocess.CompletedProcess(
                command, 0, stdout=f'# 3 "{command[-1]}"\nint alpha(void) {{return 1;}}\n'
            )

        with patch.object(source_views.subprocess, "run", side_effect=run):
            parsers = source_views.parsers(self.project, self.policy, text, self.versions, headers)
        self.assertEqual(len(parsers), 2)
        self.assertEqual(len(commands), 2)

    def test_type_rewrite_preprocessor_uses_materialized_overlay_context(self):
        source = self.proposal()
        headers = Headers.read(self.project)
        headers.texts[self.project.include[0] / "proposal.h"] = "typedef int Proposed;\n"
        roots = source_views.header_includes(self.project, headers, self.sources / "typed")
        configured = replace(self.project, include=roots, overlay_roots=roots)
        policy = replace(self.policy, cppflags=("-I" + str(self.project.include[0]),))
        with patch.object(
            typed.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, stdout="typedef int Proposed;\n")
        ) as run:
            typed.headers(configured, policy, "us")
        command = run.call_args.args[0]
        self.assertLess(command.index("-I" + str(roots[0])), command.index(policy.cppflags[0]))
        self.assertIn(str(roots[0] / "proposal.h"), run.call_args.kwargs["input"])
        self.assertFalse((self.project.include[0] / "proposal.h").exists())
        self.assertTrue(source.is_file())

    def test_submit_fold_adopts_same_overlay_before_preprocessing(self):
        source = self.proposal()
        source.write_text('#include "proposal.h"\n#if PROPOSAL\nint alpha(void) {return 1;}\n#endif\n')
        candidate = argparse.Namespace(
            source=source, function="alpha", content=source.read_bytes(), versions=self.versions
        )
        headers = Headers.read(self.project)
        with patch.object(declarations, "fold_source", wraps=declarations.fold_source) as fold:
            result = batch_fold._fold_one(self.project, self.policy, headers, candidate, batch_fold.Changes())
        self.assertIn("int alpha", result.source)
        self.assertIn(self.project.include[0] / "proposal.h", fold.call_args.args[2].texts)
        self.assertFalse((self.project.include[0] / "proposal.h").exists())
        self.assertIn("include/proposal.h", json.loads((source.parent / "overlay.json").read_text())["edits"])
