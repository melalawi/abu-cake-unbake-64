"""Splat reads landed rows cut at every function they hold, so a merge-units pass changes no extraction key."""

import unittest

from tests.kit import TempCase
from unbake import extract

HEAD = "segments:\n  - name: code\n    type: code\n    start: 0x1000\n    vram: 0x80000000\n    subsegments:\n"
FUNCTIONS = {0x80000000: "alpha", 0x80000010: "beta", 0x80000020: "gamma"}


def split(*rows: str) -> str:
    return HEAD + "".join(f"      - {row}\n" for row in rows) + "  - [0x1100]\n"


class AssemblyRowsTests(unittest.TestCase):
    def test_a_merged_row_reads_as_the_rows_it_replaced(self) -> None:
        separate = split('[0x1000, c, "alpha"]', '[0x1010, c, "beta"]', '[0x1020, asm, "gamma"]')
        merged = split('[0x1000, c, "alpha"]', '[0x1020, asm, "gamma"]')
        self.assertEqual(extract.assembly_rows(merged, FUNCTIONS), extract.assembly_rows(separate, FUNCTIONS))
        self.assertIn('      - [0x1010, asm, "beta"]\n', extract.assembly_rows(merged, FUNCTIONS))

    def test_only_landed_rows_are_cut_and_only_at_functions_inside_them(self) -> None:
        for label, rows, functions, expected in [
            ("an asm row holding two functions stays whole", ('[0x1000, asm, "alpha"]',), FUNCTIONS, 1),
            ("a landed row with no inner function", ('[0x1000, c, "alpha"]', '[0x1010, asm, "beta"]'), FUNCTIONS, 2),
            ("no function symbols", ('[0x1000, hasm, "alpha"]',), {}, 1),
            ("a landed row holding beta and gamma", ('[0x1000, hasm, "alpha"]',), FUNCTIONS, 3),
        ]:
            with self.subTest(label):
                text = extract.assembly_rows(split(*rows), functions)
                self.assertEqual(text.count(", asm, "), expected)
                self.assertNotIn(", c, ", text)
                self.assertNotIn(", hasm, ", text)

    def test_cut_rows_keep_the_row_folder(self) -> None:
        text = extract.assembly_rows(split('[0x1000, c, "dir/alpha"]'), {0x80000010: "beta"})
        self.assertIn('      - [0x1010, asm, "dir/beta"]\n', text)


class FunctionSymbolsTests(TempCase):
    def test_func_symbols_by_address_first_name_wins(self) -> None:
        path = self.root / "symbol_addrs.txt"
        path.write_text(
            "zeta = 0x80000000; // type:func\n"
            "alpha = 0x80000000; // type:func\n"
            "D_80000040 = 0x80000040;\n"
            "beta = 0x80000010; // type:func\n"
        )
        self.assertEqual(extract.function_symbols(path), {0x80000000: "alpha", 0x80000010: "beta"})


class CutBoundaryTests(unittest.TestCase):
    def test_a_function_at_a_row_start_or_its_end_is_never_a_cut(self) -> None:
        for label, inner, expected in [
            ("at the row start", 0x80000000, 0),
            ("one word in", 0x80000004, 1),
            ("last word", 0x8000000C, 1),
            ("at the next row", 0x80000010, 0),
        ]:
            with self.subTest(label):
                text = extract.assembly_rows(split('[0x1000, c, "alpha"]', '[0x1010, asm, "beta"]'), {inner: "inner"})
                self.assertEqual(text.count('"inner"'), expected)
