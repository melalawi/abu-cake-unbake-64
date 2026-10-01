"""Configured m2c invocation and context, using an executable fixture."""

import json
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from tests.decomp.test_trial import SCRATCH_ROOT, fixture
from unbake.decomp import m2c
from unbake.project.config import Held


class M2cTests(unittest.TestCase):
    def setUp(self) -> None:
        (SCRATCH_ROOT).mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=SCRATCH_ROOT)
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.project, self.policy, _ = fixture(self.directory)
        self.scratch = self.directory / "scratch"
        self.tool = self.directory / "m2c"
        self.tool.write_text(
            f"#!{sys.executable}\n" + "import json, os, pathlib, sys\n"
            "pathlib.Path('invocation.json').write_text(json.dumps({'argv': sys.argv[1:], "
            "'cwd': os.getcwd(), 'tmpdir': os.environ['TMPDIR']}))\n"
            "print('int alpha(void) { return 1; }')\n",
            encoding="utf-8",
        )
        self.tool.chmod(0o755)
        self.policy.m2c = self.tool

    def test_draft_uses_version_asm_and_project_headers(self) -> None:
        source = m2c.draft(self.project, self.policy, "alpha", "us", self.scratch)
        self.assertTrue(source.is_relative_to(self.scratch))
        self.assertEqual(source.name, "alpha.c")
        self.assertIn("NON_MATCHING", source.read_text())
        self.assertIn("typedef int s32;", source.read_text())
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

    def test_sn64_target(self) -> None:
        project = replace(self.project, compilers={"ido-7.1": replace(self.project.compilers["ido-7.1"], kind="sn64")})
        source = m2c.draft(project, self.policy, "alpha", "us", self.scratch)
        invocation = json.loads((source.parent / "invocation.json").read_text())
        self.assertIn("mips-gcc-c", invocation["argv"])

    def test_nested_headers_are_deduplicated_in_context(self) -> None:
        nested = self.project.include[0] / "nested"
        nested.mkdir()
        (nested / "value.h").write_text('#include "types.h"\nstruct Value { s32 value; };\n', encoding="utf-8")
        project = replace(self.project, include=(*self.project.include, self.project.include[0]))
        source = m2c.draft(project, self.policy, "alpha", "us", self.scratch)
        context = (source.parent / "context.c").read_text()
        self.assertEqual(context.count("typedef int s32"), 1)
        self.assertIn("struct Value", context)
        self.assertLess(context.index("typedef int s32"), context.index("struct Value"))
        self.assertIn("struct Value", source.read_text())

    def test_missing_context_include_is_named(self) -> None:
        (self.project.include[0] / "types.h").write_text('#include "missing.h"\n', encoding="utf-8")
        with self.assertRaisesRegex(Held, "context header missing.h"):
            m2c.draft(self.project, self.policy, "alpha", "us", self.scratch)

    def test_missing_or_ambiguous_asm_is_held(self) -> None:
        original = self.project.asm / "us" / "nonmatchings" / "alpha.s"
        second = self.project.asm / "us" / "alpha.s"
        second.write_bytes(original.read_bytes())
        with self.assertRaisesRegex(Held, "alpha.s.*found 2"):
            m2c.draft(self.project, self.policy, "alpha", "us", self.scratch)
        original.unlink()
        second.unlink()
        with self.assertRaisesRegex(Held, "alpha.s.*found 0"):
            m2c.draft(self.project, self.policy, "alpha", "us", self.scratch)

    def test_missing_tool_and_headers_are_named(self) -> None:
        del self.policy.m2c
        with self.assertRaisesRegex(Held, "policy.m2c"):
            m2c.draft(self.project, self.policy, "alpha", "us", self.scratch)
        self.policy.m2c = self.tool
        (self.project.include[0] / "types.h").unlink()
        with self.assertRaisesRegex(Held, "project headers are missing"):
            m2c.draft(self.project, self.policy, "alpha", "us", self.scratch)

    def test_scratch_inside_project_is_held(self) -> None:
        with self.assertRaisesRegex(Held, "scratch.*inside project.root"):
            m2c.draft(self.project, self.policy, "alpha", "us", self.project.root / "scratch")

    def test_version_and_function_are_required(self) -> None:
        for function, version, expected in (
            (None, "us", "function"),
            ("../alpha", "us", "function"),
            ("alpha", "missing", "unknown VERSION"),
        ):
            with self.subTest(function=function, version=version), self.assertRaisesRegex(Held, expected):
                m2c.draft(self.project, self.policy, function, version, self.scratch)

    def test_failed_or_empty_tool_output_is_held(self) -> None:
        self.tool.write_text(
            f"#!{sys.executable}\nimport sys\nprint('unsupported assembly', file=sys.stderr)\nsys.exit(2)\n",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(Held, "exited 2.*unsupported assembly"):
            m2c.draft(self.project, self.policy, "alpha", "us", self.scratch)
        self.tool.write_text(f"#!{sys.executable}\n", encoding="utf-8")
        with self.assertRaisesRegex(Held, "produced no draft"):
            m2c.draft(self.project, self.policy, "alpha", "us", self.scratch)


if __name__ == "__main__":
    unittest.main()
