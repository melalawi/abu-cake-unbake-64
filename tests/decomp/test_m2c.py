"""Configured m2c invocation and context with a mocked tool boundary."""

import json
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from typing import cast

from tests.decomp.support import SCRATCH_ROOT, fixture
from unbake.decomp import m2c
from unbake.decomp.draft_context import preprocess_context
from unbake.config import Held, Policy


class M2cTests(unittest.TestCase):
    def setUp(self) -> None:
        (SCRATCH_ROOT).mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=SCRATCH_ROOT)
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name).resolve()
        self.project, self.policy, _ = fixture(self.directory, case=self)
        self.scratch = self.project.work
        self.tool = self.directory / "m2c"
        self.tool.write_text(
            f"#!{sys.executable}\n" + "import json, os, pathlib, sys\n"
            "pathlib.Path('invocation.json').write_text(json.dumps({'argv': sys.argv[1:], "
            "'cwd': os.getcwd(), 'tmpdir': os.environ['TMPDIR'], "
            "'context': pathlib.Path(sys.argv[sys.argv.index('--context') + 1]).read_text()}))\n"
            "print('int alpha(void) { return 1; }')\n",
            encoding="utf-8",
        )
        self.tool.chmod(0o755)
        self.policy.m2c = self.tool

    def test_draft_uses_version_asm_and_project_headers(self) -> None:
        compiler = self.project.compilers["ido-7.1"]
        self.project = replace(self.project, compilers={compiler.id: replace(compiler, cflags=("-non_shared",))})
        source = m2c.draft(self.project, cast(Policy, self.policy), "alpha", "us", self.scratch)
        self.assertTrue(source.is_relative_to(self.scratch))
        self.assertEqual(source.name, "alpha.c")
        self.assertIn("NON_MATCHING", source.read_text())
        self.assertIn('#include "types.h"', source.read_text())
        self.assertIn("int alpha(void)", source.read_text())
        invocation = json.loads((source.parent / "invocation.json").read_text())
        self.assertIn("mips-ido-c", invocation["argv"])
        self.assertIn("--valid-syntax", invocation["argv"])
        self.assertIn("--function", invocation["argv"])
        self.assertEqual(invocation["cwd"], str(source.parent))
        self.assertEqual(invocation["tmpdir"], str(source.parent))
        context = Path(invocation["argv"][invocation["argv"].index("--context") + 1])
        self.assertIn("typedef int s32", context.read_text())
        self.assertEqual(
            (source.parent / "alpha.s").read_bytes(),
            (self.project.asm / "us" / "nonmatchings" / "alpha.s").read_bytes(),
        )

    def test_draft_compiles_with_live_sdk_include_graph_and_make_flags(self) -> None:
        from unbake.decomp.draft_input import stack_locals

        include = self.project.include[0]
        (include / "types.h").write_text(
            "#ifndef TYPES_H\n#define TYPES_H\ntypedef int s32;\ntypedef unsigned int u32;\n#endif\n"
        )
        (include / "sdk.h").write_text(
            '#ifndef SDK_H\n#define SDK_H\n#include "types.h"\n'
            "typedef union { struct { u32 w0, w1; } words; u32 alignment; } Gfx;\n#endif\n"
        )
        (include / "render.h").write_text(
            '#include "sdk.h"\n#if !defined(USE_SDK)\n#include "unavailable.h"\n#endif\n'
            "#define COMMAND 1\nextern Gfx *commands;\n"
        )
        config = self.project.root / "config.toml"
        config.write_text(config.read_text() + '[build.unit_cflags]\nalpha=["-DUSE_SDK"]\n')
        compiler = self.project.compilers["ido-7.1"]
        project = replace(
            self.project,
            compilers={compiler.id: replace(compiler, kind="sn64", cflags=("-include", "include/types.h"))},
        )
        self.tool.write_text(
            f"#!{sys.executable}\nprint('typedef int s32;\\n"
            "s32 alpha(void) { return commands->words.w0 + COMMAND; }')\n"
        )
        source = m2c.draft(project, cast(Policy, self.policy), "alpha", "us", self.scratch)
        self.assertIn('#include "render.h"', source.read_text())
        self.assertNotIn("typedef int s32;", source.read_text())
        self.assertNotIn("typedef union", source.read_text())
        # An edit after generation must reach the trial through the real header.
        render = include / "render.h"
        render.write_text(render.read_text().replace("COMMAND 1", "COMMAND 7"))
        expanded = preprocess_context(source, project, cast(Policy, self.policy), "us", "alpha")
        self.assertEqual(expanded.count("typedef int s32;"), 1)
        self.assertEqual(expanded.count("} Gfx;"), 1)
        self.assertIn("commands->words.w0 + 7", expanded)
        # m2c's stack template supplies read-only slots omitted from its locals.
        stack_output = (
            "struct _m2c_stack_beta { s32 sp10; s32 sp14; };\n"
            "s32 beta(void) {\n    s32 sp10;\n\n    sp10 = 1;\n    return sp10 + sp14;\n}\n"
        )
        locals_output = stack_locals(stack_output, expanded, "beta")
        self.assertEqual(locals_output.count("s32 sp10;"), 1)
        self.assertEqual(locals_output.count("s32 sp14;"), 1)
        read_only = stack_locals(
            "struct _m2c_stack_gamma { s32 sp18; };\ns32 gamma(void) {\n    return sp18;\n}\n",
            expanded,
            "gamma",
        )
        self.assertIn("s32 sp18;", read_only)

    def test_sn64_target(self) -> None:
        project = replace(self.project, compilers={"ido-7.1": replace(self.project.compilers["ido-7.1"], kind="sn64")})
        source = m2c.draft(project, cast(Policy, self.policy), "alpha", "us", self.scratch)
        invocation = json.loads((source.parent / "invocation.json").read_text())
        self.assertIn("mips-gcc-c", invocation["argv"])

    def test_shared_types_precede_headers_without_explicit_includes(self) -> None:
        from pycparser import c_parser  # type: ignore[import-untyped]

        for name in ("Vector3f", "Position"):
            with self.subTest(type=name):
                (self.project.include[0] / "a_game.h").write_text(
                    f"typedef struct {{ s32 id; {name} position; }} Game;\n"
                )
                (self.project.include[0] / "z_vectors.h").write_text(f"typedef struct {{ float x, y, z; }} {name};\n")
                self.tool.write_text(
                    f"#!{sys.executable}\n"
                    "import pathlib, sys\nfrom pycparser import c_parser\n"
                    "context = pathlib.Path(sys.argv[sys.argv.index('--context') + 1]).read_text()\n"
                    "c_parser.CParser().parse(context)\n"
                    "print('s32 alpha(Game *v, s32 index) { "
                    "return (s32)v->position.x + M2C_FIELD((v + index), s32 *, 0); }')\n"
                )
                source = m2c.draft(self.project, cast(Policy, self.policy), "alpha", "us", self.scratch)
                # Both the actual m2c input and the standalone candidate must parse.
                from unbake.decomp.work import compilation_project

                expanded = preprocess_context(
                    source, compilation_project(self.project, source), cast(Policy, self.policy), "us", "alpha"
                )
                c_parser.CParser().parse(expanded)
                self.assertLess(expanded.index(f"}} {name};"), expanded.index("} Game;"))

    def test_nested_headers_are_deduplicated_in_context(self) -> None:
        nested = self.project.include[0] / "nested"
        nested.mkdir()
        (nested / "value.h").write_text('#include "types.h"\nstruct Value { s32 value; };\n', encoding="utf-8")
        project = replace(self.project, include=(*self.project.include, self.project.include[0]))
        source = m2c.draft(project, cast(Policy, self.policy), "alpha", "us", self.scratch)
        context = (source.parent / "context.c").read_text()
        self.assertEqual(context.count("typedef int s32"), 1)
        initial_context = json.loads((source.parent / "invocation.json").read_text())["context"]
        self.assertEqual(initial_context.count("typedef int s32"), 1)
        self.assertIn("struct Value", initial_context)
        self.assertLess(initial_context.index("typedef int s32"), initial_context.index("struct Value"))
        self.assertNotIn("struct Value", context)
        self.assertNotIn("struct Value", source.read_text())

    def test_ambiguous_headers_without_overlay_are_held(self) -> None:
        other = self.project.root / "other-include"
        other.mkdir()
        (other / "types.h").write_text("typedef short s32;\n")
        project = replace(self.project, include=(*self.project.include, other))
        with self.assertRaisesRegex(Held, "paths.include has ambiguous header types.h"):
            m2c.draft(project, cast(Policy, self.policy), "alpha", "us", self.scratch)

    def test_missing_context_include_is_named(self) -> None:
        (self.project.include[0] / "types.h").write_text('#include "missing.h"\n', encoding="utf-8")
        with self.assertRaisesRegex(Held, "missing.h"):
            m2c.draft(self.project, cast(Policy, self.policy), "alpha", "us", self.scratch)

    def test_selected_headers_keep_include_only_dependencies(self) -> None:
        include = self.project.include[0]
        (include / "basetypes.h").write_text("typedef int s32;\n")
        (include / "types.h").write_text('#include "basetypes.h"\n')
        (include / "value.h").write_text('#include "types.h"\ntypedef struct { s32 value; } Value;\n')
        self.tool.write_text(f"#!{sys.executable}\nprint('s32 alpha(Value *v) {{ return v->value; }}')\n")
        source = m2c.draft(self.project, cast(Policy, self.policy), "alpha", "us", self.scratch)
        expanded = preprocess_context(source, self.project, cast(Policy, self.policy), "us", "alpha")
        self.assertEqual(expanded.count("typedef int s32;"), 1)
        self.assertIn("} Value;", expanded)

    def test_current_split_source_excludes_stale_assembly(self) -> None:
        original = self.project.asm / "us" / "nonmatchings" / "alpha.s"
        second = self.project.asm / "us" / "alpha.s"
        current = original.read_text()
        original.write_text("stale assembly\n")
        second.write_text(current)
        configured = self.project.version("us")
        configured.split.write_text(configured.split.read_text().replace("asm, nonmatchings/alpha", "asm, alpha"))
        source = m2c.draft(self.project, cast(Policy, self.policy), "alpha", "us", self.scratch)
        self.assertEqual((source.parent / "alpha.s").read_text(), current)
        nested = self.project.asm / "us" / "nonmatchings" / "alpha" / "alpha.s"
        nested.parent.mkdir()
        nested.write_text(current)
        configured.split.write_text(configured.split.read_text().replace("asm, alpha", "c, alpha"))
        source = m2c.draft(self.project, cast(Policy, self.policy), "alpha", "us", self.scratch)
        self.assertEqual((source.parent / "alpha.s").read_text(), current)
        configured.split.write_text(configured.split.read_text().replace("c, alpha", "asm, alpha"))
        second.unlink()
        with self.assertRaisesRegex(Held, "alpha.s.*current assembly source is missing"):
            m2c.draft(self.project, cast(Policy, self.policy), "alpha", "us", self.scratch)

    def test_entry_label_uses_build_address_correspondence(self) -> None:
        original = self.project.asm / "us" / "nonmatchings" / "alpha.s"
        dump = self.project.build_link("us").resolve() / "splat_symbols.csv"
        for label, address, expected in (
            ("alpha_auto", "80001000", None),
            ("renamed_entry", "80001000", None),
            ("alpha_auto", "80001004", "expected one entry label.*found 0"),
        ):
            with self.subTest(label=label, address=address):
                original.write_text(f"glabel {label}\nbnez $v0, tail\nnop\nglabel tail\njr $ra\nnop\n")
                dump.write_text(f"name,vram_start\n{label},{address}\ntail,80001004\n")
                if expected:
                    with self.assertRaisesRegex(Held, expected):
                        m2c.draft(self.project, cast(Policy, self.policy), "alpha", "us", self.scratch)
                    continue
                source = m2c.draft(self.project, cast(Policy, self.policy), "alpha", "us", self.scratch)
                prepared = (source.parent / "alpha.s").read_text()
                self.assertIn("glabel alpha\n", prepared)
                self.assertIn("bnez $v0, .L_tail", prepared)
                self.assertNotIn(label, prepared)
                invocation = json.loads((source.parent / "invocation.json").read_text())
                self.assertEqual(invocation["argv"][invocation["argv"].index("--function") + 1], "alpha")
                self.assertIn("int alpha(void)", source.read_text())
        dump.unlink()
        with self.assertRaisesRegex(Held, "entry correspondence for alpha"):
            m2c.draft(self.project, cast(Policy, self.policy), "alpha", "us", self.scratch)

    def test_missing_tool_and_headers_are_named(self) -> None:
        del self.policy.m2c
        with self.assertRaisesRegex(Held, "policy.m2c"):
            m2c.draft(self.project, cast(Policy, self.policy), "alpha", "us", self.scratch)
        self.policy.m2c = self.tool
        (self.project.include[0] / "types.h").unlink()
        with self.assertRaisesRegex(Held, "project headers are missing"):
            m2c.draft(self.project, cast(Policy, self.policy), "alpha", "us", self.scratch)

    def test_scratch_inside_project_is_held(self) -> None:
        with self.assertRaisesRegex(Held, "scratch.*outside project.root"):
            m2c.draft(self.project, cast(Policy, self.policy), "alpha", "us", self.project.root / "scratch")

    def test_version_and_function_are_required(self) -> None:
        for function, version, expected in (
            (None, "us", "function"),
            ("../alpha", "us", "function"),
            ("alpha", "missing", "unknown VERSION"),
        ):
            with self.subTest(function=function, version=version), self.assertRaisesRegex(Held, expected):
                m2c.draft(self.project, cast(Policy, self.policy), function, version, self.scratch)

    def test_failed_or_empty_tool_output_is_held(self) -> None:
        self.tool.write_text(
            f"#!{sys.executable}\nimport sys\nprint('unsupported assembly', file=sys.stderr)\nsys.exit(2)\n",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(Held, "exited 2.*unsupported assembly"):
            m2c.draft(self.project, cast(Policy, self.policy), "alpha", "us", self.scratch)
        self.tool.write_text(f"#!{sys.executable}\n", encoding="utf-8")
        with self.assertRaisesRegex(Held, "produced no draft"):
            m2c.draft(self.project, cast(Policy, self.policy), "alpha", "us", self.scratch)


if __name__ == "__main__":
    unittest.main()
