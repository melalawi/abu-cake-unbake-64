"""A real public SDK helper's inline macro stays inside generic declarations."""

import hashlib
import shutil
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.kit import TESTS
from unbake import cache, cdecl
from unbake.config import Held
from unbake.project.headers import FileBlob, Graph, Search, TreeView
from unbake.typemap import split

ROOT = (TESTS / "typemap/test_stdarg_declarations.py").parents[1] / "fixtures/stdarg_declarations/include"
HEADER = ROOT / "stdarg.h"
HELPER = "__unbake_stdarg_take"
DECORATION = "__unbake_stdarg_inline"


class StdargDeclarationTests(unittest.TestCase):
    def setUp(self):
        cache.configure(memory_bytes=1 << 20)
        cache.forget(["typemap-statements", "graph.projection", "decl.names", "tree.bytes"])
        self.data = HEADER.read_bytes()
        self.text = self.data.decode()

    def gcc_body(self):
        return self.text[self.text.index("typedef char") : self.text.index("#elif defined(__UNBAKE_STDARG_IDO)")]

    def test_real_public_sdk_projection_retains_the_helper_definition(self):
        row = Graph.contents({HEADER: self.data}, (ROOT,)).projection(HEADER)
        self.assertIsNone(row.parse_error)
        self.assertEqual(row.ordinary, {"__builtin_next_arg", HELPER})
        self.assertEqual(row.typedefs, {"__unbake_stdarg_target"})
        self.assertEqual(row.declarations.functions, {HELPER})
        self.assertEqual(row.uses, {"va_list"})
        self.assertEqual(len(row.statements), 1)
        self.assertEqual(row.statements[0], self.text[self.text.index("#ifndef") :].rstrip())
        self.assertIn("static " + DECORATION + " char *", row.statements[0])
        self.assertEqual(row.statements[0].count("return slot;"), 2)

    def test_equal_public_sdk_reads_and_scans_once_with_shared_macro_context(self):
        self.assertEqual(len(self.data), 2952)
        self.assertEqual(
            hashlib.sha256(self.data).hexdigest(), "3faee1771ab247e48c11bfa72568cca1d6b154f045961630defbb2b40a04d64f"
        )
        graphs = [
            Graph(TreeView("stdarg", {HEADER: FileBlob(HEADER, None)}, (ROOT,)), Search(include_roots=(ROOT,)))
            for _ in range(2)
        ]
        reads = []
        original_read = Path.read_bytes

        def read(path):
            data = original_read(path)
            if path == HEADER:
                reads.append(len(data))
            return data

        with (
            patch.object(Path, "read_bytes", read),
            patch.object(cdecl.NameParser, "parse", autospec=True, side_effect=cdecl.NameParser.parse) as parses,
            patch.object(cdecl, "declaration_context", wraps=cdecl.declaration_context) as contexts,
            patch.object(split, "declaration_context", wraps=cdecl.declaration_context) as split_context,
            patch.object(cdecl, "NAME_TOKEN", wraps=cdecl.NAME_TOKEN) as names,
            patch.object(split, "SOURCE_TOKEN", wraps=cdecl.SOURCE_TOKEN) as scanner,
            patch.object(split, "_split", wraps=split._split) as splits,
            patch.object(split, "_function_body", wraps=split._function_body) as classify,
        ):
            rows = [graph.projection(HEADER) for graph in graphs for _ in range(2)]
        self.assertTrue(all(row.parse_error is None for row in rows))
        self.assertEqual(reads, [2952])
        self.assertEqual(parses.call_count, 3)  # Complete header plus two function signatures.
        self.assertEqual(contexts.call_count, 2)  # One name analysis and one split, shared by both signatures.
        self.assertEqual([len(call.args[0]) for call in contexts.call_args_list], [2952, 2952])
        self.assertEqual(split_context.call_count, 1)
        self.assertEqual(names.finditer.call_count, 2)  # Declaration positions for use-before-definition checks.
        self.assertEqual(names.findall.call_count, 9)  # Six object macro replacements and three parser inputs.
        self.assertEqual(scanner.finditer.call_count, 1)
        self.assertEqual(splits.call_count, 1)
        self.assertEqual(classify.call_count, 2)

    def test_real_gcc_body_boundaries_and_clean_source_preserve_offsets(self):
        clean, decorations = cdecl.declaration_context(self.text)
        self.assertEqual(clean, cdecl.declaration_source(self.text))
        self.assertEqual(len(clean), 2952)
        self.assertEqual(
            [i for i, c in enumerate(clean) if c == "\n"], [i for i, c in enumerate(self.text) if c == "\n"]
        )
        self.assertIn(DECORATION, decorations)
        self.assertIn("static " + DECORATION + " char *", clean)
        body = self.gcc_body()
        pieces = split.statements(body)
        self.assertEqual(len(pieces), 9)
        definition = next(piece for piece in pieces if piece.startswith("static "))
        self.assertEqual(definition, body[body.index("static ") : body.index("\n#define va_arg")].rstrip())
        self.assertEqual(cdecl.declarations(body).functions, {HELPER})
        self.assertNotIn(DECORATION, cdecl.declarations(body).uses)
        self.assertTrue(pieces[-1].startswith("#define va_copy"))

    def test_generic_empty_and_inline_definitions_do_not_need_compiler_names(self):
        for replacement in ("", "inline", "__inline__", "__extension__ __inline"):
            with self.subTest(replacement=replacement):
                text = (
                    "#ifdef STRICT\n#define DECL_HELPER\n#else\n#define DECL_HELPER "
                    + replacement
                    + " /* qualified */\n#endif\nstatic DECL_HELPER char *take(char **p) { return *p; }\n"
                    "extern int later;"
                )
                row = cdecl.declarations(text)
                self.assertEqual(row.declared, {"take", "later"})
                self.assertEqual(row.functions, {"take"})
                self.assertEqual(row.exports, {"later"})
                self.assertEqual(row.uses, set())
                self.assertEqual(len(split.statements(text)), 3)
        continued = "#define DECL_HELPER \\\ninline\nstatic DECL_HELPER char *take(char **p) { return *p; }"
        self.assertEqual(cdecl.declarations(continued).functions, {"take"})
        self.assertEqual(len(split.statements(continued)), 2)

    def test_unknown_ambiguous_type_linkage_and_function_macros_are_not_skipped(self):
        function = "static DECL_HELPER char *take(char **p) { return *p; }"
        invalid = (
            function,
            "/*\n#define DECL_HELPER inline\n*/\n" + function,
            "#define DECL_HELPER int\n" + function,
            "#if ONE\n#define DECL_HELPER inline\n#else\n#define DECL_HELPER int\n#endif\n" + function,
            "#define DECL_HELPER extern\n" + function,
            "#define DECL_HELPER(x) inline\n" + function,
            "#define DECL_HELPER inline\n#undef DECL_HELPER\n" + function,
            function + "\n#define DECL_HELPER inline\n",
        )
        for text in invalid:
            with self.subTest(text=text), self.assertRaisesRegex(Held, "header declaration"):
                cdecl.declarations(text)
        row = cdecl.declarations("#define Word int\nextern Word *value;")
        self.assertEqual(row.uses, {"Word"})
        self.assertEqual(row.exports, {"value"})
        broken = "#define DECL_HELPER inline\n" + function[:-1]
        self.assertIsNotNone(Graph.contents({HEADER: broken}, (ROOT,)).projection(HEADER).parse_error)
        with self.assertRaisesRegex(Held, "incomplete"):
            split.statements(broken)

    def test_original_and_retained_sdk_compile_in_strict_and_inline_modes(self):
        compiler = shutil.which("cc")
        if compiler is None:
            self.skipTest("native C compiler unavailable")
        row = Graph.contents({HEADER: self.data}, (ROOT,)).projection(HEADER)
        self.assertIsNone(row.parse_error)
        reconstructed = "\n".join(row.statements) + "\n"
        consumer = (
            "\nint read_argument(int count, ...) {\n"
            " va_list ap; int value; va_start(ap, count); value = va_arg(ap, int); va_end(ap); return value;\n}\n"
        )
        with patch.object(subprocess, "run", wraps=subprocess.run) as compiles:
            for mode in ("c89", "gnu99"):
                for text in ("#include <stdarg.h>\n", reconstructed):
                    result = subprocess.run(
                        [
                            compiler,
                            "-m32",
                            "-std=" + mode,
                            "-Werror",
                            "-fsyntax-only",
                            "-D__UNBAKE_STDARG_GCC",
                            "-I",
                            str(ROOT),
                            "-x",
                            "c",
                            "-",
                        ],
                        input=text + consumer,
                        text=True,
                        capture_output=True,
                        check=False,
                    )
                    self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(compiles.call_count, 4)


if __name__ == "__main__":
    unittest.main()
