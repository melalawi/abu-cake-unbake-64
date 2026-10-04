"""Explicit storage intervals can prove a reference absent from aligned ROM text."""

import unittest
from unittest.mock import patch

from tests.layout.test_rodata import Object, words
from unbake.objects.literal_layout import arrange


class ExplicitPoolReferenceTests(unittest.TestCase):
    def test_authoritative_reference_table(self):
        for label, anchors, target, reason in (
            ("explicit", [(0, 0x80003000, 4)], {}, None),
            ("no authority", [], {}, "missing aligned"),
            ("short authority", [(0, 0x80003000, 2)], {}, "missing aligned"),
            ("conflicting authority", [(0, 0x80003000, 4), (0, 0x80003004, 4)], {}, "missing aligned"),
            ("disagreeing reference", [(0, 0x80003000, 4)], {0: 0x3C018000, 4: 0xC4203004}, "disagrees with explicit"),
            ("changed instruction", [(0, 0x80003000, 4)], {0: 0x3C028000, 4: 0xC4203000}, "instruction differs"),
        ):
            obj = Object()
            with (
                self.subTest(label=label),
                patch("unbake.objects.literal_layout.storage", return_value=anchors),
                patch("unbake.objects.literal_layout.write") as publish,
            ):

                def read(address, size):
                    return words(0x3F800000)[:size]

                if reason:
                    with self.assertRaisesRegex(ValueError, reason):
                        arrange(obj, ".rdata", target, 0x80001000, read, persist=False)
                else:
                    self.assertEqual(arrange(obj, ".rdata", target, 0x80001000, read, persist=False), 0x80003000)
                publish.assert_not_called()
