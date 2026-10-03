"""Local text relocations prove table pointer encoding without a layout guess."""

import unittest
from unittest.mock import Mock, patch

from tests.layout.test_rodata import Object, words
from unbake.layout.rodata_owners import Span, proved_tables
from unbake.project_tools.literal_layout import arrange
from unbake.project_tools.rodata import relocated, table_pointer_bias


class TableEncodingTests(unittest.TestCase):
    def object(self):
        return Object(
            data=words(0, 4),
            code=words(0x3C010000, 0x24220000),
            data_rels=[(at, 2, dict(name="", value=0, section=0)) for at in (0, 4)],
        )

    def test_exact_pointer_encoding_table(self):
        for label, actual, resident, expected in (
            ("virtual", words(0x80401000, 0x80401004), words(0x80401000, 0x80401004), 0),
            ("physical", words(0x80401000, 0x80401004), words(0x00401000, 0x00401004), 0x80000000),
            ("wrong entry", words(0x80401000, 0x80401004), words(0x00401000, 0x00401008), None),
            ("arbitrary delta", words(0x80401000, 0x80401004), words(0x00402000, 0x00402004), None),
            ("mixed encoding", words(0x80401000, 0x80401004), words(0x00401000, 0x80401004), None),
            ("empty", b"", b"", None),
            ("partial", b"abc", b"abc", None),
            ("different sizes", words(0x80401000), words(0x00401000, 0x00401004), None),
        ):
            with self.subTest(label=label):
                self.assertEqual(table_pointer_bias(actual, resident), expected)

    def test_unconfigured_encoding_is_proved_and_normalized(self):
        raw = words(0x00401000, 0x00401004)
        targets = {0: 0x3C018000, 4: 0x24223000}
        for emit in (False, True):
            obj = self.object()
            read = Mock(return_value=raw)
            with self.subTest(emit=emit), patch("unbake.project_tools.literal_layout.write") as publish:
                self.assertEqual(
                    arrange(obj, ".rdata", targets, 0x80401000, read, emit_resident=emit, persist=False), 0x80003000
                )
                expected = raw if emit else words(0x80401000, 0x80401004)
                self.assertEqual(relocated(obj, ".rdata", 0x80401000), expected)
                publish.assert_not_called()
        tables = proved_tables(
            self.object(),
            "alpha",
            words(*targets.values()),
            0x80401000,
            bytes(0x40) + raw,
            [Span(0x80003000, 0x40, 0x48, 0, "resident")],
        )
        self.assertEqual(tables, [("alpha", 0x80003000, 0x80003008)])

    def test_wrong_table_still_refuses(self):
        obj = self.object()
        with patch("unbake.project_tools.literal_layout.write") as publish:
            with self.assertRaisesRegex(ValueError, "bytes.*disagree"):
                arrange(
                    obj,
                    ".rdata",
                    {0: 0x3C018000, 4: 0x24223000},
                    0x80401000,
                    Mock(return_value=words(0x401000, 0x401008)),
                    emit_resident=True,
                    persist=False,
                )
            publish.assert_not_called()

    def test_scalar_bytes_cannot_use_table_encoding(self):
        obj = Object(data=words(0x80401000))
        with self.assertRaisesRegex(ValueError, "bytes.*disagree"):
            arrange(
                obj,
                ".rdata",
                {0: 0x3C018000, 4: 0xC4203000},
                0x80401000,
                Mock(return_value=words(0x401000)),
                persist=False,
            )
