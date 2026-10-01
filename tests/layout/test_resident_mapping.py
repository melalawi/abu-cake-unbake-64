import struct
import unittest

from tests.layout.test_rodata import Object, words
from unbake.project_tools.layout import resident, resident_mappings


class ResidentMappingTests(unittest.TestCase):
    def test_runtime_alias_literals_and_biased_jump_table(self) -> None:
        interval = dict(address=0x80200400, start=0x20, end=0x28)
        mapping = dict(address=0x80001000, start=0x40, end=0x48, table_entry_bias=0x80000000)
        mappings = resident_mappings([mapping])
        for data, rels, expected in [
            (words(0x3F800000, 0x3F800000), [], words(0x3F800000, 0x3F800000)),
            (
                words(4, 8),
                [(at, 2, dict(name=".text", value=0, section=0)) for at in (0, 4)],
                words(0x200404, 0x200408),
            ),
        ]:
            with self.subTest(data=data):
                obj = Object(data=data, data_rels=rels)
                image = bytearray(0x48)
                struct.pack_into(">II", image, 0x20, 0x3C018000, 0xC4201000)
                image[0x40:0x48] = expected
                with self.assertRaisesRegex(ValueError, "bytes disagree"):
                    resident(obj, interval, bytes(image), ".rdata")
                self.assertEqual(resident(obj, interval, bytes(image), ".rdata", mappings), 0x80001000)
                image[0x40] ^= 1
                with self.assertRaisesRegex(ValueError, "bytes disagree"):
                    resident(obj, interval, bytes(image), ".rdata", mappings)
                with self.assertRaisesRegex(ValueError, "ambiguous"):
                    resident(obj, interval, bytes(image), ".rdata", mappings * 2)
