"""cdecl.resumable_parse: a unit sharing a long prefix with the previous one parses exactly as a full parse."""

import unittest
from typing import ClassVar

from pycparser import c_generator  # type: ignore[import-untyped]

from unbake import cdecl

HEADER = "".join(f"typedef struct S{i} {{ int a; char b[{i + 1}]; }} T{i};\nextern T{i} g{i};\n" for i in range(40))


class ResumableParseTests(unittest.TestCase):
    CASES: ClassVar[dict[str, tuple[str, str]]] = {
        "typedef from the prefix types the rest": (HEADER + "T3 *f(T1 x);\n", HEADER + "extern T39 last;\n"),
        "braces and calls in the prefix": (
            HEADER + "int table[2] = { 1, 2 };\nvoid h(void) { if (1) { g0.a = 1; } }\nint a;\n",
            HEADER + "int table[2] = { 1, 2 };\nvoid h(void) { if (1) { g0.a = 1; } }\nlong b;\n",
        ),
        "a parameter named like a typedef after the prefix": (
            HEADER + "void k(int T5);\nT5 *p;\n",
            HEADER + "void k(int T6);\nT6 *q;\n",
        ),
        "no shared prefix": ("int a;\n", "long b;\n"),
    }

    def setUp(self) -> None:
        cdecl._PREFIXES.clear()
        cdecl._previous.clear()

    def test_cases(self) -> None:
        generator = c_generator.CGenerator()
        for label, texts in self.CASES.items():
            with self.subTest(label):
                cdecl._PREFIXES.clear()
                cdecl._previous.clear()
                for text in (*texts, *texts):
                    expected = generator.visit(cdecl.parser({}).parse(text))
                    self.assertEqual(generator.visit(cdecl.resumable_parse(text, {})), expected)

    def test_a_shared_prefix_is_parsed_once(self) -> None:
        first, second = self.CASES["typedef from the prefix types the rest"]
        cdecl.resumable_parse(first, {})
        cdecl.resumable_parse(second, {})
        self.assertEqual(len(cdecl._PREFIXES), 1)
        ((_, prefix),) = cdecl._PREFIXES
        self.assertTrue(first.startswith(prefix) and second.startswith(prefix))


if __name__ == "__main__":
    unittest.main()
