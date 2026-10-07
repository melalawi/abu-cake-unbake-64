"""prefixes: a unit pass resumed at a checkpoint gives exactly the pass over the whole unit."""

import unittest
from collections.abc import Callable
from typing import ClassVar

from unbake import cache, cdecl, prefixes
from unbake.typemap import declarations

HEADER = "".join(
    f"typedef struct S{i} {{\n    int a;\n    char b[{i + 1}];\n}} T{i};\nextern T{i} g{i};\n" for i in range(300)
)


class CheckpointTests(unittest.TestCase):
    def test_last_top_level_line_start_before_any_unsafe_text(self) -> None:
        body = "struct A {\n    int a;\n};\nint b;\n"
        cases = [
            ("after the last top-level declaration", body, len(body), 0, len(body)),
            ("inside braces counts only top level", "struct A {\n    int a;\n", 22, 0, 0),
            ("limit cuts the text", body, 25, 0, len("struct A {\n    int a;\n};\n")),
            ("a quote stops checkpoints", 'int a;\nchar *s = "x";\nint b;\n', 30, 0, len("int a;\n")),
            ("a directive stops checkpoints", "int a;\n#pragma x\nint b;\n", 24, 0, len("int a;\n")),
            ("a comment stops checkpoints", "int a;\n/* c */\nint b;\n", 22, 0, len("int a;\n")),
            ("no checkpoint", "int a", 5, 0, 0),
            ("start is already checkpointed", body + "int c;\n", len(body) + 7, len(body), len(body) + 7),
        ]
        for label, text, limit, start, expected in cases:
            with self.subTest(label):
                self.assertEqual(prefixes.checkpoint(text, limit, start), expected)


class ConcatenatedPassTests(unittest.TestCase):
    RESTS = (
        "int f(int x) { if (x) { return x; } return 0; }\nT1 *p;\n",
        "static int table[2] = { 1, 2 };\nint __attribute__((aligned(8))) q;\n__extension__ long r;\n",
        "struct Local { T2 t; };\nvoid g(void) { struct Local l; }\n",
        "",
    )
    PASSES: ClassVar[dict[str, Callable[[str], str]]] = {
        "clean": lambda text: declarations.clean(text),
        "clean with markers": lambda text: declarations.clean(text, line_markers=True),
        "body blanking": declarations._unit_bodies_blanked,
        "declaration source": cdecl.declaration_source,
    }

    def test_resumed_pass_equals_the_whole_pass(self) -> None:
        for label, transform in self.PASSES.items():
            with self.subTest(label):
                cache.configure(memory_bytes=32 * 1024 * 1024)
                prefixes.forget()
                for rest in (*self.RESTS, *reversed(self.RESTS)):
                    text = HEADER + rest
                    self.assertEqual(prefixes.concatenated(label, text, transform), transform(text))

    def test_a_shared_header_is_transformed_once(self) -> None:
        cache.configure(memory_bytes=32 * 1024 * 1024)
        prefixes.forget()
        seen: list[int] = []

        def transform(text: str) -> str:
            seen.append(len(text))
            return text.upper()

        for rest in self.RESTS[:3]:
            prefixes.concatenated("count", HEADER + rest, transform)
        # The first unit is whole, the second keeps the header, the third reads only its rest.
        self.assertEqual(seen[0], len(HEADER + self.RESTS[0]))
        self.assertLess(seen[-1], len(self.RESTS[2]) + 64)


class ResumedParseTests(unittest.TestCase):
    def test_parse_equals_a_full_parse(self) -> None:
        from pycparser import c_generator  # type: ignore[import-untyped]

        generator = c_generator.CGenerator()
        cache.configure(memory_bytes=32 * 1024 * 1024)
        prefixes.forget()
        for rest in ("T3 *f(T1 x);\n", "extern T299 last;\n", "void k(int T5);\nT5 *p;\n", "int a;\n"):
            text = HEADER + rest
            self.assertEqual(
                generator.visit(cdecl.resumable_parse(text, {})), generator.visit(cdecl.parser({}).parse(text))
            )

    def test_an_unparsable_unit_reports_the_whole_parse_error(self) -> None:
        cache.configure(memory_bytes=32 * 1024 * 1024)
        prefixes.forget()
        cdecl.resumable_parse(HEADER + "int a;\n", {})
        bad = HEADER + "int b\n"
        with self.assertRaises(Exception) as resumed:
            cdecl.resumable_parse(bad, {})
        with self.assertRaises(Exception) as whole:
            cdecl.parser({}).parse(bad)
        self.assertEqual(str(resumed.exception), str(whole.exception))


if __name__ == "__main__":
    unittest.main()
