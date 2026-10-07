"""The route19 header stays complete through the public projection and splitter."""

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
from unbake.typemap.declaration_evidence import _body

ROOT = (TESTS / "typemap/test_header_split.py").parents[1] / "fixtures/header_split/include"
HEADER = ROOT / "shared/func_80214624_de_closed.h"


class HeaderSplitTests(unittest.TestCase):
    def setUp(self):
        cache.configure(memory_bytes=1 << 20)
        cache.forget(["typemap-statements", "graph.projection", "decl.names", "tree.bytes"])
        self.data = HEADER.read_bytes()
        self.text = self.data.decode()

    def test_real_public_projection_does_not_hold_valid_inline_header(self):
        row = Graph.contents({HEADER: self.data}, (ROOT,)).projection(HEADER)
        self.assertIsNone(row.parse_error)
        self.assertEqual(len(row.statements), 18)
        self.assertEqual(len(row.ordinary), 16)
        self.assertEqual(row.typedefs, {"ApproachLocation"})

    def test_real_public_projection_reads_1604_bytes_and_splits_once(self):
        self.assertEqual(len(self.data), 1604)
        self.assertEqual(
            hashlib.sha256(self.data).hexdigest(), "80a81cdffae0502179a0edc180b0251c11227db0ffd295a29c5bf2d758f98e86"
        )
        graphs = [
            Graph(TreeView("route19", {HEADER: FileBlob(HEADER, None)}, (ROOT,)), Search(include_roots=(ROOT,)))
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
            patch.object(split, "_split", wraps=split._split) as splits,
            patch.object(split, "SOURCE_TOKEN", wraps=cdecl.SOURCE_TOKEN) as scanner,
            patch.object(split, "_function_body", wraps=split._function_body) as classifier,
            patch.object(cdecl, "declarations", wraps=cdecl.declarations) as declarations,
        ):
            rows = [graph.projection(HEADER) for graph in graphs for _ in range(2)]
        for row in rows:
            self.assertIsNone(row.parse_error)
            self.assertEqual(len(row.statements), 18)
            self.assertEqual(len(row.ordinary), 16)
            self.assertEqual(row.typedefs, {"ApproachLocation"})
            self.assertEqual(row.includes[0].name, "types.h")
            self.assertEqual(row.includes[1].name, "common/unused.h")
            self.assertEqual(
                row.statements[-1], self.text[self.text.index("static inline") : self.text.rindex("}") + 1]
            )
        self.assertEqual(reads, [1604])
        self.assertEqual(splits.call_count, 1)
        self.assertEqual(scanner.finditer.call_count, 1)
        self.assertEqual(classifier.call_count, 1)
        self.assertEqual(declarations.call_count, 1)

    def test_outer_guard_and_inline_conditional_bodies_are_kept_whole(self):
        self.assertEqual(split.statements(self.text), (self.text.rstrip(),))
        block = (
            "#if VERSION_US\nstatic inline int f(void) {\n#if DETAIL\nreturn 1;\n"
            "#else\nreturn 2;\n#endif\n}\n#else\nstatic inline int f(void) { return 3; }\n#endif"
        )
        self.assertEqual(
            split.statements("extern int before;\n" + block + "\nextern int after;"),
            ("extern int before;", block, "extern int after;"),
        )

    def test_aggregates_initializers_and_quoted_or_comment_braces_keep_boundaries(self):
        expected = (
            "typedef struct { int value; } Box;",
            "struct Packed __attribute__((packed)) { int value; };",
            "static const Box boxes[] = {{1}, {2}};",
            "int (*callback)(int);",
            'static inline const char *braces(void) { /* } ; #endif */ return "}\\";{"; }',
            "struct Packed make(void) { struct Packed p = {1}; return p; }",
            "extern int after;",
        )
        self.assertEqual(split.statements("\n".join(expected)), expected)
        text = "/*\n#endif\n*/\n" + expected[-2]
        self.assertEqual(split.statements(text), (expected[-2],))
        continued_comment = "// comment \\\n#endif\n" + expected[-2]
        self.assertEqual(split.statements(continued_comment), (expected[-2],))

    def test_incomplete_declarations_and_conditionals_still_refuse(self):
        incomplete = (
            "static inline void broken(void) { if (1) { }",
            "typedef struct { int value; } Box",
            "static const int values[] = {1, 2}",
            "extern int broken(",
            "#if ENABLED\nstatic inline int f(void) { return 1; }",
            "#if ENABLED\nextern int missing_semicolon\n#endif",
        )
        for text in incomplete:
            with self.subTest(text=text), self.assertRaisesRegex(Held, "incomplete"):
                split.statements(text)
        for text in ("#endif", "#else", "#elif ENABLED", "extern int x);", "}"):
            with self.subTest(text=text), self.assertRaisesRegex(Held, "unmatched"):
                split.statements(text)

    def test_original_and_split_header_compile_with_real_actor_vec3_context(self):
        compiler = shutil.which("cc")
        if compiler is None:
            self.skipTest("native C compiler unavailable")
        body = _body(self.text)
        pieces = split.statements(body)
        prefix = self.text[: self.text.index(body)]
        reconstructed = prefix + "\n".join(pieces) + "\n#endif\n"
        consumer = "\nvoid exercise(Actor_func_80214624_de *a, Vec3 *p, s32 *room) {\n approach(a, a, a, p, room);\n}\n"
        with patch.object(subprocess, "run", wraps=subprocess.run) as compiles:
            for text in ('#include "shared/func_80214624_de_closed.h"\n', reconstructed):
                result = subprocess.run(
                    [compiler, "-std=c99", "-Werror", "-fsyntax-only", "-I", str(ROOT), "-x", "c", "-"],
                    input=text + consumer,
                    text=True,
                    capture_output=True,
                    check=False,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(compiles.call_count, 2)
        self.assertEqual(len(pieces), 18)


if __name__ == "__main__":
    unittest.main()
