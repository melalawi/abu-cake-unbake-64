"""Small header fixtures and synthetic integrity vectors."""

import struct
import tempfile
import unittest
import zlib
from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest.mock import patch

from unbake.project import header, rom
from unbake.project.config import Held

GOLDEN = (("us", "6aa4dde7e3e2f4e7", "4e42584500", "EXAMPLEONE", 0x80071000),)
# Independent reference results for repeating max/sign-bit/one/rotate-31 words.
VECTORS = (
    ("6101", "00000001", 0xF8E8CDDC, 0x947A4D8D),
    ("7102", "00000002", 0xF8E8CDDC, 0x947A4D8D),
    ("6102/7101", "00000003", 0xF8E8CDDC, 0x947A4D8D),
    ("6103/7103", "00000004", 0xA3B6E759, 0x72776759),
    ("6105/7105", "00000005", 0xDF4B7436, 0xDF43F5BB),
    ("6106/7106", "00000006", 0xE40D0F9E, 0xA44185E8),
)
WORDS = bytes.fromhex("ffffffff80000000000000010000001f")


def image(vector: Any = VECTORS[2]) -> bytes:
    _, patch, crc1, crc2 = vector
    data = bytearray(0x101000)
    struct.pack_into(">6I", data, 0, 0x80371240, 15, 0x80000400, 0x1444, crc1, crc2)
    data[0x20:0x34] = b"Example Game        "
    data[0x3B:0x40] = bytes.fromhex("4e45584500")
    data[0x750:0x850] = WORDS * 16
    data[0xFFC:0x1000] = bytes.fromhex(patch)
    data[0x1000:] = WORDS * (0x100000 // len(WORDS))
    return bytes(data)


BOOTCODES = {zlib.crc32(image(vector)[0x40:0x1000]): vector[0] for vector in VECTORS}


class HeaderTests(unittest.TestCase):
    def test_reference_header(self) -> None:
        for name, crc, identity, title, entry in GOLDEN:
            with self.subTest(name=name, title=title):
                title_hex = "4558414d504c454f4e4520202020202020202020"
                data = bytes.fromhex(
                    "803712400000000f"
                    + f"{entry:08x}"
                    + "00001444"
                    + crc
                    + "0000000000000000"
                    + title_hex
                    + "00000000000000"
                    + identity
                )
                facts = header.decode(data)
                self.assertEqual((facts.pi, facts.clock_rate, facts.entry), (0x80371240, 15, entry))
                self.assertEqual((facts.libultra, facts.release_revision, facts.release_letter), (0x1444, 20, "D"))
                self.assertEqual((facts.crc1, facts.crc2), struct.unpack(">II", bytes.fromhex(crc)))
                self.assertEqual((facts.title, header.label(facts)), (title, name))

    def test_all_destinations_categories_and_revision_boundaries(self) -> None:
        expected = dict(
            zip(
                "7ABCDEFGHIJKLNPSUWXYZ",
                (
                    "beta",
                    "all",
                    "br",
                    "cn",
                    "de",
                    "us",
                    "fr",
                    "gw-ntsc",
                    "nl",
                    "it",
                    "jp",
                    "kr",
                    "gw-pal",
                    "ca",
                    "eu",
                    "es",
                    "au",
                    "nordic",
                    "eu-x",
                    "eu-y",
                    "eu-z",
                ),
                strict=False,
            )
        )
        self.assertEqual(header.DESTINATIONS, expected)
        data = bytearray(image()[:64])
        for destination, label in expected.items():
            for category in "NDCEZ":
                for revision in (0, 1, 255):
                    with self.subTest(destination=destination, category=category, revision=revision):
                        data[0x3B] = ord(category)
                        data[0x3E] = ord(destination)
                        data[0x3F] = revision
                        facts = header.decode(data)
                        self.assertEqual(facts.category, category)
                        self.assertEqual(header.label(facts), label + (f"-rev{revision}" if revision else ""))

    def test_cic_variants_and_independent_crc_vectors(self) -> None:
        for vector in VECTORS:
            with self.subTest(cic=vector[0]):
                data = image(vector)
                facts = header.parse(data, BOOTCODES)
                self.assertEqual((facts.cic, facts.crc1, facts.crc2), (vector[0], vector[2], vector[3]))
                self.assertEqual(header.checksum(data + b"ignored", vector[0]), vector[2:])

    def test_named_refusals(self) -> None:
        valid = image()
        cases = [
            (b"", "size"),
            (valid[:63], "size"),
            (b"bad!" + valid[4:], "pi"),
            (valid[:64], "ipl3"),
            (valid[:0x1000], "crc_data"),
            (valid[:64] + bytes(0xFC0) + valid[4096:], "cic"),
        ]
        for offset, value, field in (
            (0x3B, 0, "category"),
            (0x3E, 0, "destination"),
            (0x20, 255, "title"),
            (0x3C, 255, "game_code"),
            (0x10, valid[0x10] ^ 1, "crc1"),
            (0x14, valid[0x14] ^ 1, "crc2"),
        ):
            data = bytearray(valid)
            data[offset] = value
            cases.append((bytes(data), field))
        for data, field in cases:
            with self.subTest(field=field, size=len(data)), self.assertRaisesRegex(Held, "header." + field):
                header.parse(data, BOOTCODES)
        with self.assertRaisesRegex(Held, "cic"):
            header.checksum(valid, "unknown")
        with self.assertRaisesRegex(Held, "destination"):
            header.label(replace(header.decode(valid), region="?"))

    def test_byte_orders_tails_and_load_does_not_write(self) -> None:
        valid = image()
        with patch.object(header, "RETAIL", BOOTCODES), tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            for width in (1, 2, 4):
                with self.subTest(width=width):
                    swapped = b"".join(valid[i : i + width][::-1] for i in range(0, len(valid), width))
                    path = root / f"order-{width}"
                    path.write_bytes(swapped)
                    loaded = rom.load(path)
                    self.assertEqual(loaded.data, valid)
                    self.assertEqual(path.read_bytes(), swapped)
                    self.assertEqual(loaded.header.cic, "6102/7101")
            for size in (0, 1, 2, 3, 4, 31, 63):
                with self.subTest(size=size), self.assertRaises(Held):
                    rom.normalise(valid[:size])
            for width in (2, 4):
                for tail in range(1, width):
                    swapped = b"".join(valid[i : i + width][::-1] for i in range(0, len(valid), width)) + bytes(tail)
                    with self.subTest(width=width, tail=tail), self.assertRaisesRegex(Held, "size"):
                        rom.normalise(swapped)
            with self.assertRaisesRegex(Held, "rom.size"):
                rom.normalise(valid + b"x")
            with self.assertRaisesRegex(Held, "missing"):
                rom.load(root / "missing")

    def test_explicit_bootcode_table_controls_verification(self) -> None:
        valid = image()
        for table, field in (
            (None, "header.bootcodes"),
            ({}, "unknown IPL3"),
            (header.RETAIL, "unknown IPL3"),
            ({zlib.crc32(valid[0x40:0x1000]): "unknown"}, "unsupported variant"),
        ):
            with self.subTest(table=table), self.assertRaisesRegex(Held, field):
                header.parse(valid, table)
