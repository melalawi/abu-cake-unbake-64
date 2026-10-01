"""Byte order, VERSION labels and measured-code similarity."""

import struct
import tempfile
import unittest
import zlib
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

from unbake.project import header, rom
from unbake.project.config import Held

BOOTCODES = {zlib.crc32(bytes(0xFC0)): "6102/7101"}
CODE = bytes.fromhex("27bdffe8afbf0014008010210c000004000000008fbf001403e0000827bd0018")
OTHER_CODE = bytes.fromhex("3c0280003442000124420001ac8200008c820000104000010000000003e00008")


def cartridge(
    *, region: str = "E", revision: int = 0, code: Any = "RW", seed: int = 0, instructions: Any = CODE
) -> Any:
    data = bytearray(0x101000)
    struct.pack_into(">4I", data, 0, 0x80371240, 15, 0x80001000, 0x1444)
    data[0x20:0x34] = b"Example Game        "
    data[0x3B:0x40] = b"N" + code.encode() + region.encode() + bytes([revision])
    data[0x1000 : 0x1000 + len(instructions)] = instructions
    data[0x1100] = seed
    struct.pack_into(">2I", data, 0x10, *header.checksum(data, "6102/7101"))
    return bytes(data)


def inventory(*cartridges: Any, ranges: Any = ((0x1000, 0x1020),)) -> Any:
    return {item.path: [SimpleNamespace(start=start, end=end) for start, end in ranges] for item in cartridges}


class RomTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        table = patch.object(header, "RETAIL", BOOTCODES)
        table.start()
        self.addCleanup(table.stop)

    def load(self, name: str, data: bytes) -> rom.Rom:
        path = self.root / name
        path.write_bytes(data)
        return rom.load(path)

    def test_normalises_byte_orders_without_writing_inputs(self) -> None:
        normal = cartridge()
        for width in (1, 2, 4):
            with self.subTest(width=width):
                data = b"".join(normal[i : i + width][::-1] for i in range(0, len(normal), width))
                loaded = self.load(f"order-{width}", data)
                self.assertEqual(loaded.data, normal)
                self.assertEqual(loaded.path.read_bytes(), data)
                self.assertEqual(
                    (loaded.header.title, loaded.header.game_code, loaded.header.libultra),
                    ("Example Game", "RW", 0x1444),
                )

    def test_invalid_files_are_refused_by_name(self) -> None:
        valid = cartridge()
        corrupt = bytearray(valid)
        corrupt[0x1000] ^= 1
        for data, field in (
            (b"text", "magic"),
            (bytes.fromhex("37804012") + bytes(61), "size"),
            (valid[:64], "ipl3"),
            (bytes(corrupt), "crc1"),
        ):
            with self.subTest(field=field), self.assertRaisesRegex(Held, "bad.*" + field):
                self.load("bad.txt", data)
        with self.assertRaisesRegex(Held, "missing.z64"):
            rom.load(self.root / "missing.z64")

    def test_version_labels_revisions_and_named_rename_refusals(self) -> None:
        first = self.load("first", cartridge(region="X"))
        revision = replace(first, header=replace(first.header, revision=255))
        self.assertEqual(rom.version_names([revision], {})[revision.path], "eu-x-rev255")
        self.assertEqual(rom.version_names([first], {"eu-x": "pal-special"})[first.path], "pal-special")
        for carts, renames, field in (
            ([first, first], {}, "VERSION eu-x"),
            ([first], {"eu-x": "../bad"}, "--version-name eu-x"),
            ([first], {"missing": "new"}, "--version-name missing"),
        ):
            with self.subTest(field=field), self.assertRaisesRegex(Held, field):
                rom.version_names(carts, renames)

    def test_similarity_uses_ranges_and_discards_registers_and_relocations(self) -> None:
        first = self.load("first", cartridge())
        modified = bytes.fromhex("27bdffd8afbf000c00a018210c001234000000008fbf000c03e0000827bd0028")
        second = self.load("second", cartridge(seed=137, instructions=modified))
        scores = rom.same_game([first, second], inventory(first, second), 0.9)
        self.assertEqual(
            scores, {(first, first): 1.0, (second, second): 1.0, (first, second): 1.0, (second, first): 1.0}
        )
        # Move the executable ranges beyond the former fixed comparison window.
        offset = 0x210000
        far = [replace(item, data=bytes(offset) + item.data[0x1000:0x1020]) for item in (first, second)]
        self.assertEqual(rom.same_game(far, inventory(*far, ranges=((offset, offset + 32),)), 0.9)[tuple(far)], 1.0)

    def test_identical_assets_do_not_make_disjoint_code_related(self) -> None:
        first = self.load("first", cartridge())
        second = self.load("second", cartridge(instructions=OTHER_CODE))
        with self.assertRaisesRegex(Held, "first.*code similarity 0.000 below 0.10"):
            rom.same_game([first, second], inventory(first, second), 0.1)
        self.assertEqual(rom.same_game([first], inventory(first), 0.1)[first, first], 1.0)

    def test_same_game_named_refusals(self) -> None:
        first = self.load("first", cartridge())
        second = replace(first, path=self.root / "second", sha1="second")
        cases = [
            ([], {}, 0.5, "rompath"),
            ([first], None, 0.5, "inventories"),
            ([first, replace(second, sha1=first.sha1)], inventory(first, second), 0.5, "duplicate sha1.*first"),
            (
                [first, replace(second, header=replace(first.header, game_code="BX"))],
                inventory(first, second),
                0.5,
                "NBX differs from NRW",
            ),
            (
                [first, replace(second, header=replace(first.header, category="D"))],
                inventory(first, second),
                0.5,
                "game code",
            ),
            ([first], {}, 0.5, "first.*detected code ranges"),
            ([first], {first.path: []}, 0.5, "detected code ranges"),
        ]
        cases.extend(
            ([first], inventory(first), value, "init_same_game_similarity")
            for value in (None, True, 0, -1, 1.1, float("nan"))
        )
        for start, end in ((-4, 32), (1, 32), (0, 31), (32, 32), (0, len(first.data) + 4), (False, 32), (None, 32)):
            cases.append(([first], inventory(first, ranges=((start, end),)), 0.5, "invalid word range"))
        cases.append(([first], inventory(first, ranges=((0x1000, 0x1010), (0x1010, 0x1020))), 0.5, "code shingles"))
        for carts, ranges, threshold, field in cases:
            with self.subTest(field=field, threshold=threshold), self.assertRaisesRegex(Held, field):
                rom.same_game(carts, ranges, threshold)

    def test_shingles_word_and_window_boundaries(self) -> None:
        for size, expected in ((0, 0), (28, 0), (31, 0), (32, 1), (35, 1), (36, 2)):
            with self.subTest(size=size):
                data = (CODE + OTHER_CODE)[:size]
                self.assertEqual(len(rom.shingles(data)), expected)
        # COP1 arithmetic keeps funct bits; COP1 register transfers discard them.
        for left, right, equal in ((0x46000000, 0x46000001, False), (0x44000000, 0x44000001, True)):
            with self.subTest(left=left, right=right):
                self.assertEqual(
                    rom.shingles(struct.pack(">I", left) * 8) == rom.shingles(struct.pack(">I", right) * 8), equal
                )
