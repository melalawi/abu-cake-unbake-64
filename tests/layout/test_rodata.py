import struct
import tempfile
import unittest
from typing import Any

from unbake.families.gcc import Gcc
from unbake.families.ido import Ido
from unbake.config import Held
from unbake.objects.rodata import fragment, placement, relocated


def words(*values: int) -> Any:
    return b"".join(struct.pack(">I", value) for value in values)


DEFAULT_DATA = bytes.fromhex("3f800000")
DEFAULT_CODE = words(0x3C010000, 0xC4200000)
DEFAULT_FAMILY = Gcc()


def Object(
    section: Any = ".rdata",
    data: Any = DEFAULT_DATA,
    code: Any = DEFAULT_CODE,
    text_rels: Any = None,
    data_rels: Any = (),
) -> Any:
    """A minimal ELF fixture read by the production object parser."""
    from unbake.objects.elf import Object as ElfObject

    names = ["", ".text", section or ".unused", ".symtab", ".strtab", ".rel.text", ".rel.data", ".shstrtab"]
    shstrings = b"\0"
    name_offsets = []
    for name in names:
        name_offsets.append(len(shstrings))
        shstrings += name.encode() + b"\0"
    symbol = dict(name=section, value=0, section=1)
    text_rels = [(0, 5, symbol), (4, 6, symbol)] if text_rels is None else text_rels
    rels = [text_rels, data_rels]
    strings = b"\0"
    symbols = bytes(16)
    indexes = {}
    relocation_data = []
    for entries in rels:
        packed = b""
        for offset, kind, item in entries:
            key = (item["name"], item["value"], item["section"])
            if key not in indexes:
                indexes[key] = len(symbols) // 16
                name_offset = len(strings)
                strings += str(item["name"]).encode() + b"\0"
                symbols += struct.pack(">IIIBBH", name_offset, item["value"], 0, 0x10, 0, item["section"] + 1)
            packed += struct.pack(">II", offset, indexes[key] << 8 | kind)
        relocation_data.append(packed)
    contents = [b"", code, data if section else b"", symbols, strings, *relocation_data, shstrings]
    types = [0, 1, 1, 2, 3, 9, 9, 3]
    image = bytearray(52)
    image[:16] = b"\x7fELF\x01\x02\x01" + bytes(9)
    headers = []
    for index, content in enumerate(contents):
        image.extend(bytes((-len(image)) % 4))
        offset = len(image)
        image.extend(content)
        headers.append(
            (
                name_offsets[index],
                types[index],
                6 if index == 1 else 0,
                0,
                offset,
                len(content),
                4 if index == 3 else 3 if index in (5, 6) else 0,
                1 if index in (3, 5) else 2 if index == 6 else 0,
                4,
                16 if index == 3 else 8 if index in (5, 6) else 0,
            )
        )
    image.extend(bytes((-len(image)) % 4))
    table = len(image)
    for header in headers:
        image.extend(struct.pack(">10I", *header))
    struct.pack_into(">HHIIIIIHHHHHH", image, 16, 1, 8, 1, 0, 0, table, 0, 52, 0, 0, 40, 8, 7)
    with tempfile.NamedTemporaryFile(suffix=".o") as temporary:
        temporary.write(image)
        temporary.flush()
        return ElfObject(temporary.name)


class RodataTests(unittest.TestCase):
    def test_pool_partition_boundaries(self) -> None:
        for family in (Gcc(), Ido()):
            for size in (0, 1, 2, 3, 4, 7, 8, 15, 16):
                with self.subTest(family=type(family).__name__, size=size):
                    obj = Object(section=family.rodata_section(), data=bytes(size))
                    pools = family.literal_pools(obj)
                    self.assertEqual(sum(pool.size for pool in pools), size)
                    self.assertEqual(family.jump_tables(obj), [])
            symbol = dict(name="local", value=4, section=0)
            obj = Object(section=family.rodata_section(), data=bytes(19), data_rels=[(4, 2, symbol), (8, 2, symbol)])
            self.assertEqual([(p.offset, p.size) for p in family.jump_tables(obj)], [(4, 8)])
            self.assertEqual([(p.offset, p.size) for p in family.literal_pools(obj)], [(0, 4), (12, 7)])
            self.assertEqual(relocated(obj, family.rodata_section(), 0x80400000)[4:12], words(0x80400004, 0x80400004))

    def test_plurality_sign_extension_symbol_addend_and_pairing(self) -> None:
        for low in (0x7FFF, 0x8000, 0xFFFF):
            with self.subTest(low=low):
                obj = Object(code=words(0x3C010000, 0xC4200000 | low))
                target = {0: 0x3C018001, 4: 0xC4200000 | low}
                self.assertEqual(placement(obj, ".rdata", target), (0x80010000, 0))
        obj = Object(code=words(0x3C010000, 0xC4200000) * 3)
        symbol = dict(name=".rdata", value=4, section=1)
        entries = [(at, kind, symbol) for at, kind in ((0, 5), (4, 6), (8, 5), (12, 6), (16, 5), (20, 6))]
        obj = Object(code=words(0x3C010000, 0xC4200000) * 3, text_rels=entries)
        target = {0: 0x3C018000, 4: 0xC4201004, 8: 0x3C018000, 12: 0xC4201004, 16: 0x3C018000, 20: 0xC4202004}
        self.assertEqual(placement(obj, ".rdata", target), (0x80001000, 1))
        target[16] = None
        self.assertEqual(placement(obj, ".rdata", target), (0x80001000, 0))
        entries.insert(1, (0, 5, symbol))
        obj = Object(code=words(0x3C010000, 0xC4200000) * 3, text_rels=entries)
        self.assertEqual(placement(obj, ".rdata", target), (0x80001000, 0))

    def test_named_relocation_refusals(self) -> None:
        symbol = dict(name=".rdata", value=0, section=1)
        cases = [
            ([], {}, "base"),
            ([(0, 5, symbol)], {0: 0x3C018000}, "LO16"),
            ([(4, 6, symbol)], {4: 0xC4201000}, "HI16"),
            ([(0, 4, symbol)], {0: 0x3C018000}, "unsupported"),
            ([(0, 5, symbol)], {}, "target_words"),
            ([(8, 5, symbol)], {8: 0x3C018000}, ".text"),
        ]
        for rels, target, reason in cases:
            with self.subTest(reason=reason), self.assertRaisesRegex(ValueError, reason):
                placement(Object(text_rels=rels), ".rdata", target)
        obj = Object(
            code=words(0x3C010000, 0xC4200000) * 2,
            text_rels=[(0, 5, symbol), (4, 6, symbol), (8, 5, symbol), (12, 6, symbol)],
        )
        with self.assertRaisesRegex(ValueError, "tied"):
            placement(obj, ".rdata", {0: 0x3C018000, 4: 0xC4201000, 8: 0x3C018000, 12: 0xC4202000})
        for at, kind, section, reason in [
            (1, 2, 0, "aligned"),
            (4, 2, 0, "aligned"),
            (0, 4, 0, "R_MIPS_32"),
            (0, 2, 1, "R_MIPS_32"),
        ]:
            with self.subTest(at=at, kind=kind, section=section), self.assertRaisesRegex(Held, reason):
                Gcc().jump_tables(Object(data_rels=[(at, kind, dict(name="label", value=0, section=section))]))

    def test_fragment_required_facts_and_selector(self) -> None:
        for section in (".rdata", ".rodata"):
            row = dict(object="obj/src/function.o", section=section, address=0x80001000)
            self.assertIn(f"obj/src/function.o({section})", fragment([row]))
            for key in row:
                bad = dict(row)
                del bad[key]
                with self.subTest(missing=key), self.assertRaisesRegex(ValueError, key):
                    fragment([bad])
            for key, value in [
                ("object", "../x.o"),
                ("section", ".text"),
                ("address", None),
                ("address", True),
                ("address", 0x100000000),
            ]:
                with self.subTest(key=key, value=value), self.assertRaisesRegex(ValueError, key):
                    fragment([{**row, key: value}])
        self.assertEqual(fragment([]), "")


if __name__ == "__main__":
    unittest.main()
