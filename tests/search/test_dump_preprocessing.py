"""Diagnostic dumps must compile the same guarded function as draft trials."""

import os
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import patch

from unbake.decomp.explain import function_dump, gcc_input, render
from unbake.families.gcc.allocation import allocation
from unbake.search.core import preprocess


class DumpPreprocessingTests(unittest.TestCase):
    def test_guarded_function_and_helpers_have_separate_allocation_evidence(self) -> None:
        with tempfile.TemporaryDirectory(dir=os.environ["TMPDIR"]) as temporary:
            work = Path(temporary)
            source = work / "selected.c"
            body = (
                "static int helper(int x) { return x + 1; }\n"
                "int selected(int x) { switch (x) { case 1: return helper(x); default: return 0; } }\n"
            )
            cpp = shutil.which("cpp")
            self.assertIsNotNone(cpp)
            for kind in ("sn64", "gcc"):
                for guarded in (False, True):
                    with self.subTest(kind=kind, guarded=guarded):
                        original = "#ifdef NON_MATCHING\n" + body + "#endif\n" if guarded else body
                        source.write_text(original)
                        project = NS(root=work, compiler_for=lambda _, kind=kind: NS(kind=kind, cc=cpp))
                        with (
                            patch("unbake.project.makefile.flags", return_value=[]),
                            patch("unbake.project.makefile.recipe", return_value=NS(cpp=cpp, cppflags=["-P"])),
                            patch("unbake.project.makefile.host_executable", return_value=cpp),
                        ):
                            expanded, _ = gcc_input(project, NS(), source, "us-rev1", work, preserve_lines=False)
                            search_input = preprocess(project, NS(), source, "us-rev1", time.monotonic() + 10)
                        for text in (expanded, search_input):
                            self.assertIn("int selected(int x)", text)
                            self.assertIn("static int helper(int x)", text)
                        self.assertEqual(source.read_text(), original)
                        # Tiny allocator rows, bounded by adjacent function headers.
                        local = (
                            ";; Function helper\nRegister 80 used 99 times across 1 insns\n"
                            ";; Function selected (selected, funcdef_no=1)\n"
                            "Register 80 used 4 times across 8 insns\n"
                            ";; Function following\nRegister 80 used 77 times across 1 insns\n"
                        )
                        global_text = (
                            ";; Function helper\n;; 1 regs to allocate: 80\n"
                            ";; Register dispositions:\n80 in 2\n"
                            ";; Function selected\n;; 1 regs to allocate: 80\n"
                            ";; Register dispositions:\n80 in 16\n"
                            ";; Function following\n;; Register dispositions:\n80 in 3\n"
                        )
                        result = allocation(
                            {
                                "lreg": function_dump(local, "selected"),
                                "greg": function_dump(global_text, "selected"),
                            }
                        )
                        self.assertEqual(result.pseudos[0].references, 4)
                        self.assertEqual(result.pseudos[0].hard, 16)
                        self.assertIn("allocation rank 0: pseudo 80 in 16", render(result))
