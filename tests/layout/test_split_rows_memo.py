"""Split row indexes are shared while their inputs hold, and never hash every row to find out."""

from types import SimpleNamespace
from unittest.mock import patch

from tests.kit import TempCase
from unbake import cache
from unbake.layout import split


class Row(SimpleNamespace):
    def __hash__(self) -> int:  # type: ignore[override]
        raise AssertionError("a row was hashed")


class RowsMemoTests(TempCase):
    def setUp(self) -> None:
        super().setUp()
        cache.forget()
        self.asm = self.root / "asm"
        (self.asm / "us").mkdir(parents=True)
        self.parsed_rows = [
            Row(path="f", kind="c", start=0, end=4, aliases=("f", "f_alias")),
            Row(path="g", kind="asm", start=4, end=8, aliases=("g",)),
        ]
        version = SimpleNamespace(split=self.root / "split.yaml", symbols=self.root / "symbols.txt")
        self.project = SimpleNamespace(asm=self.asm, version=lambda name: version)

    def rows(self) -> tuple:
        current = self.parsed_rows
        with patch.object(cache, "parsed", side_effect=lambda kind, paths, parse, extra=None: current):
            return split._rows(self.project, "us")

    def test_reuse_and_invalidation(self) -> None:
        first = self.rows()
        self.assertIs(self.rows(), first)
        with patch.object(cache, "parsed", side_effect=lambda kind, paths, parse, extra=None: self.parsed_rows):
            index = split.owners_by_alias(self.project, "us")
            self.assertIs(split.owners_by_alias(self.project, "us"), index)
        self.assertEqual(sorted(index), ["f", "f_alias", "g"])
        cases = [
            (
                "new parsed rows (split or symbols changed)",
                lambda: setattr(self, "parsed_rows", list(self.parsed_rows)),
            ),
            ("a file renamed into the asm tree", lambda: (self.asm / "us" / "h.s").write_text("")),
            ("the extract step forgot the memo", lambda: cache.forget([split.ASM_ROWS])),
        ]
        for label, change in cases:
            with self.subTest(label):
                before = self.rows()
                change()
                after = self.rows()
                self.assertIsNot(after, before)
                self.assertEqual(after, before)

    def test_without_an_asm_tree(self) -> None:
        self.project.asm = None
        first = self.rows()
        self.assertIs(self.rows(), first)
        self.assertEqual([row.path for row in first], ["f", "g"])
