"""Data proofs use the compiled entry owned by a unit, including source aliases."""

import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from tests.layout.test_rodata import Object, words
from unbake.match import data_symbols
from unbake.project.config import Held


class DataEntryOwnershipTests(unittest.TestCase):
    def object(self, entries):
        external = dict(name="data", value=0, section=-1)
        obj = Object(
            code=words(0x3C080000, 0x25080000, 0x3C090000, 0x25290000),
            text_rels=[(0, 5, external), (4, 6, external), (8, 5, external), (12, 6, external)],
        )
        table = next(iter(obj.symbols))
        obj.symbols[table].extend(
            dict(table=table, index=20 + n, name=name, value=value, size=size, info=0x12, section=1)
            for n, (name, value, size) in enumerate(entries)
        )
        return obj

    def test_entry_selection_and_extent_table(self):
        cases = (
            ("exact", [("unit", 0, 8)], ("unit",), 0x80003000),
            ("explicit alias", [("native", 0, 8)], ("unit", "native"), 0x80003000),
            ("sole source spelling", [("native", 0, 8)], ("unit",), 0x80003000),
            ("helper outside entry", [("unit", 0, 0), ("helper", 8, 8)], ("unit",), 0x80003000),
            ("entry after helper", [("helper", 0, 8), ("unit", 8, 8)], ("unit",), 0x80003000),
            ("ambiguous", [("one", 0, 8), ("two", 8, 8)], ("unit",), None),
            ("missing", [], ("unit",), None),
        )
        project = SimpleNamespace(version=lambda v: SimpleNamespace(symbols=Path("symbols")))
        for label, entries, aliases, expected in cases:
            obj = self.object(entries)
            high, low = (0x3C098000, 0x25293000) if label == "entry after helper" else (0x3C088000, 0x25083000)
            row = SimpleNamespace(aliases=aliases)
            with (
                self.subTest(label=label),
                patch.object(data_symbols.split, "functions", return_value=[row]),
                patch.object(data_symbols.split, "symbols", return_value=("", {})),
                patch.object(data_symbols.split, "words", return_value=words(high, low)),
                patch.object(data_symbols, "Object", return_value=obj),
            ):
                if expected is None:
                    with self.assertRaisesRegex(Held, "expected one compiled text entry"):
                        data_symbols.needs(project, "unit", "eu", Path("candidate.o"))
                else:
                    result = data_symbols.needs(project, "unit", "eu", Path("candidate.o"))
                    self.assertEqual([(r.name, r.address) for r in result], [("data", expected)])

    def test_owning_entry_instruction_difference_still_refuses(self):
        obj = self.object([("unit", 0, 8)])
        with self.assertRaisesRegex(ValueError, "instruction differs"):
            data_symbols.placements(obj, words(0x3C098000, 0x25083000), set(), 0, 8)
