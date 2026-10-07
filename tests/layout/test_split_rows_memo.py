"""Split row indexes are shared while their inputs hold, and never hash every row to find out."""

from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import patch

from tests.kit import TempCase
from unbake import cache
from unbake.layout import split


class RowsMemoTests(TempCase):
    def setUp(self) -> None:
        super().setUp()
        cache.forget()
        self.computations = []
        self.asm = self.root / "asm"
        (self.asm / "us").mkdir(parents=True)
        self.parsed_rows = [
            split.Function("us", "f", 0, 4, 0x80001000, "f", "c", ("f", "f_alias"), (("f", 0),)),
            split.Function("us", "g", 4, 8, 0x80001004, "g", "asm", ("g",), (("g", 0),)),
        ]
        version = SimpleNamespace(split=self.root / "split.yaml", symbols=self.root / "symbols.txt")
        self.project = SimpleNamespace(asm=self.asm, version=lambda name: version)

    def rows(self) -> tuple:
        current = self.parsed_rows
        with (
            patch.object(cache, "parsed", side_effect=lambda kind, paths, parse, extra=None: current),
            patch.object(cache, "remember", wraps=cache.remember) as retained,
        ):
            result = split._rows(self.project, "us")
        self.computations.append(retained.call_count)
        return result

    def test_reuse_and_invalidation(self) -> None:
        first = self.rows()
        self.assertEqual(self.rows(), first)
        self.assertEqual(self.computations, [1, 0])
        with patch.object(cache, "parsed", side_effect=lambda kind, paths, parse, extra=None: self.parsed_rows):
            index = split.owners_by_alias(self.project, "us")
            again = split.owners_by_alias(self.project, "us")
            self.assertEqual(again, index)
            again["f"].clear()
            self.assertEqual(len(split.owners_by_alias(self.project, "us")["f"]), 1)
        self.assertEqual(sorted(index), ["f", "f_alias", "g"])
        cases = [
            (
                "new parsed rows (split or symbols changed)",
                lambda: setattr(
                    self, "parsed_rows", [replace(self.parsed_rows[0], aliases=("f",)), self.parsed_rows[1]]
                ),
            ),
            ("a file renamed into the asm tree", lambda: (self.asm / "us" / "h.s").write_text("")),
            ("the extract step forgot the memo", lambda: cache.forget([split.ASM_ROWS])),
        ]
        for label, change in cases:
            with self.subTest(label):
                before = self.rows()
                change()
                after = self.rows()
                self.assertEqual(after, tuple(self.parsed_rows))
                self.assertGreaterEqual(self.computations[-1], 1, "changed inputs invalidate the retained computation")
                if label.startswith("new parsed"):
                    self.assertNotEqual(after, before)

    def test_without_an_asm_tree(self) -> None:
        self.project.asm = None
        first = self.rows()
        self.assertEqual(self.rows(), first)
        self.assertEqual(self.computations, [1, 0])
        self.assertEqual([row.path for row in first], ["f", "g"])
