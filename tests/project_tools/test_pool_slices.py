"""Disjoint private pools replace exactly one load selector per slice."""

import struct
import subprocess
import tempfile
import unittest
from pathlib import Path

from tests.decomp.support import LINKER, assemble
from unbake.project_tools.elf import Object
from unbake.project_tools.extract import instruction_symbols, pool_rows, raw_storage, unit_ranges
from unbake.project_tools.layout import transfer_private, transfer_selectors


class PoolSliceTests(unittest.TestCase):
    def test_two_published_units_link_with_local_pool_symbols_and_external_references(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            objects = []
            selectors = []
            for number, name in enumerate(("alpha", "beta")):
                address = 0x80003000 + number * 0x100
                text_address = 0x80002000 + number * 0x100
                label = f"unbake_rodata_{address:08X}_4"
                obj = Object(
                    assemble(
                        root,
                        name,
                        f".set noreorder\n.text\n.globl {name}\n{name}:\n"
                        f"lui $at,%hi({label})\nlwc1 $f0,%lo({label})($at)\n"
                        "lui $v0,%hi(external)\naddiu $v0,$v0,%lo(external)\njr $ra\nnop\n"
                        f".section .rdata\n.globl {label}\n{label}: .word 0x3f800000\n"
                        f".data\n.word {label},{name},external\n",
                    )
                )
                image = bytearray(0x44)
                image[0x20:0x38] = struct.pack(
                    ">6I", 0x3C018000, 0xC4200000 | (address & 65535), 0x3C020000, 0x24420000, 0x03E00008, 0
                )
                image[0x40:0x44] = bytes.fromhex("3f800000")
                slices = [dict(start=0x40, end=0x44, address=address, path=f"rodata/{name}/{address:08X}")]
                sections = transfer_private(obj, dict(start=0x20, end=0x38, address=text_address), bytes(image), slices)
                rebuilt = Object(obj.path)
                for sym_index, symbols in rebuilt.symbols.items():
                    first = rebuilt.sections[sym_index][7]
                    self.assertTrue(all(s["info"] >> 4 == 0 for s in symbols[:first]))
                    self.assertTrue(all(s["info"] >> 4 != 0 for s in symbols[first:]))
                    section_symbols = [s for s in symbols if s["section"] == rebuilt.section(sections[0])]
                    self.assertEqual({s["info"] for s in section_symbols}, {3, 16})
                data = rebuilt.section(".data")
                assert data is not None
                self.assertEqual([s["name"] for _, _, s in rebuilt.relocations(data)], [label, name, "external"])
                selectors.extend(
                    [
                        f".text_{name} 0x{text_address:X} : {{ {obj.path}(.text) }}",
                        transfer_selectors(
                            f".pool_{name} 0x{address:X} : {{ obj/asm/data/{slices[0]['path']}.rodata.o(.rodata) }}",
                            str(obj.path),
                            slices,
                            sections,
                        ),
                    ]
                )
                objects.append(str(obj.path))
            script = root / "link.ld"
            script.write_text(
                "external = 0x80004000;\nSECTIONS {\n" + "\n".join(selectors) + "\n.data : SUBALIGN(1) { *(.data) } }"
            )
            result = subprocess.run(
                [LINKER, "-T", str(script), "-o", str(root / "linked.elf"), *objects], capture_output=True, text=True
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            linked = Object(root / "linked.elf")
            data = linked.section(".data")
            assert data is not None
            self.assertEqual(
                linked.content(data),
                struct.pack(">6I", 0x80003000, 0x80002000, 0x80004000, 0x80003100, 0x80002100, 0x80004000),
            )
            for name in ("alpha", "beta"):
                pool = linked.section(f".pool_{name}")
                assert pool is not None
                self.assertEqual(linked.content(pool), bytes.fromhex("3f800000"))

    def test_data_symbol_omitted_from_csv_uses_original_instruction_words(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "text.s").write_text(
                "/* 20 80001020 3C05800C */ lui $a1,%hi(interior)\n"
                "/* 24 80001024 24A5A52C */ addiu $a1,$a1,%lo(interior)\n"
            )
            self.assertEqual(instruction_symbols(root, {}), {"interior": 0x800BA52C})
            with self.assertRaisesRegex(ValueError, "conflicting instruction address"):
                instruction_symbols(root, {"interior": 0x800CA52C})

    def test_retained_double_at_absolute_eight_byte_boundary_keeps_exact_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            # ROM start is four bytes off an eight-byte boundary. A .double
            # directive would align its contents relative to this object.
            image = bytes(4) + struct.pack(">Id", 0x12345678, 4294967296.0)
            obj = Object(
                assemble(Path(directory), "retained", raw_storage(dict(start=4, end=16, section=".data"), image))
            )
            data = obj.section(".data")
            assert data is not None
            self.assertEqual(obj.content(data), image[4:16])

    def test_structural_rows_keep_disjoint_owner_and_shared_storage(self) -> None:
        yaml = (
            "segments:\n  - name: main\n    type: code\n    start: 0x20\n    vram: 0x80002000\n"
            "    subsegments:\n      - [0x20, c, alpha]\n      - [0x30, asm, beta]\n"
            '      - [0x40, rodata, "rodata/alpha/80002020"]\n'
            '      - [0x44, rodata, "rodata/shared/80002024"]\n'
            '      - [0x48, rodata, "rodata/alpha/80002028"]\n  - [0x4C]\n'
        )
        rows = pool_rows(yaml)
        self.assertEqual([row["owner"] for row in rows], ["alpha", None, "alpha"])
        ranges = unit_ranges(yaml)
        self.assertEqual(
            [(r["start"], r["end"]) for r in ranges["alpha"]["rodata_slices"]], [(0x40, 0x44), (0x48, 0x4C)]
        )

    def test_duplicate_literal_references_transfer_to_disjoint_sections(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            obj = Object(
                assemble(
                    root,
                    "alpha",
                    ".set noreorder\n.text\n.globl alpha\nalpha:\n"
                    "lui $at,%hi(literal)\nlwc1 $f0,%lo(literal)($at)\n"
                    "lui $at,%hi(literal)\nlwc1 $f2,%lo(literal)($at)\njr $ra\nnop\n"
                    ".section .rdata\nliteral: .word 0x3f800000\n",
                )
            )
            target = [0x3C018000, 0xC4203000, 0x3C018000, 0xC4225000, 0x03E00008, 0]
            image = bytearray(0x84)
            image[0x20:0x38] = struct.pack(">6I", *target)
            image[0x40:0x44] = image[0x80:0x84] = bytes.fromhex("3f800000")
            slices = [
                dict(start=0x40, end=0x44, address=0x80003000, path="rodata/alpha/80003000"),
                dict(start=0x80, end=0x84, address=0x80005000, path="rodata/alpha/80005000"),
            ]
            names = transfer_private(obj, dict(start=0x20, end=0x38, address=0x80002000), bytes(image), slices)
            rebuilt = Object(obj.path)
            self.assertEqual(names, [".unbake_pool_80003000", ".unbake_pool_80005000"])
            for name in names:
                index = rebuilt.section(name)
                assert index is not None
                self.assertEqual(rebuilt.content(index), bytes.fromhex("3f800000"))
            text = rebuilt.section(".text")
            assert text is not None
            self.assertEqual(
                [s["section"] for _, _, s in rebuilt.relocations(text)],
                [rebuilt.section(names[0])] * 2 + [rebuilt.section(names[1])] * 2,
            )
            script = "\n".join("obj/asm/data/" + row["path"] + ".rodata.o(.rodata)" for row in slices)
            linked = transfer_selectors(script, "obj/src/alpha.o", slices, names)
            self.assertNotIn("obj/asm", linked)
            self.assertEqual(linked.count("obj/src/alpha.o"), 2)
            self.assertEqual(
                transfer_private(rebuilt, dict(start=0x20, end=0x38, address=0x80002000), bytes(image), slices), names
            )

    def test_two_private_owners_link_with_local_section_symbols(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            objects = []
            for number, name in enumerate(("alpha", "beta")):
                obj = Object(
                    assemble(
                        root,
                        name,
                        f".set noreorder\n.text\n.globl {name}\n{name}:\n"
                        "lui $at,%hi(literal)\nlwc1 $f0,%lo(literal)($at)\njr $ra\nnop\n"
                        ".section .rdata\nliteral: .word 0x3f800000\n",
                    )
                )
                address = 0x80003000 + 4 * number
                image = bytearray(0x44)
                image[0x20:0x30] = struct.pack(">4I", 0x3C018000, 0xC4200000 | address & 65535, 0x03E00008, 0)
                image[0x40:0x44] = bytes.fromhex("3f800000")
                names = transfer_private(
                    obj,
                    dict(start=0x20, end=0x30, address=0x80002000),
                    bytes(image),
                    [dict(start=0x40, end=0x44, address=address)],
                )
                rebuilt = Object(obj.path)
                for table, symbols in rebuilt.symbols.items():
                    first_global = rebuilt.sections[table][7]
                    self.assertTrue(all(symbol["info"] >> 4 == 0 for symbol in symbols[:first_global]))
                    self.assertTrue(all(symbol["info"] >> 4 != 0 for symbol in symbols[first_global:]))
                    self.assertTrue(
                        all(
                            symbol["info"] >> 4 == 0
                            for symbol in symbols
                            if symbol["section"] == rebuilt.section(names[0])
                        )
                    )
                objects.append(str(obj.path))
            linked = subprocess.run(
                [LINKER, "-r", "-o", str(root / "combined.o"), *objects], capture_output=True, text=True
            )
            self.assertEqual(linked.returncode, 0, linked.stderr)
            combined = Object(root / "combined.o")
            self.assertIsNotNone(combined.section(".unbake_pool_80003000"))
            self.assertIsNotNone(combined.section(".unbake_pool_80003004"))

    def test_string_biased_table_and_literal_use_both_compiler_sections(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            obj = Object(
                assemble(
                    Path(directory),
                    "mixed",
                    ".set noreorder\n.text\n.globl alpha\nalpha:\n"
                    "lui $at,%hi(literal)\nlwc1 $f0,%lo(literal)($at)\n"
                    "lui $at,%hi(string)\naddiu $a0,$at,%lo(string)\n"
                    "lui $at,%hi(table)\naddiu $v0,$at,%lo(table)\n"
                    "case: jr $ra\nnop\n.section .rdata\nliteral: .word 0x3f800000\n"
                    '.section .rodata\nstring: .asciz "hello"\n.align 2\ntable: .word case,case\n',
                )
            )
            image = bytearray(0xC4)
            image[0x20:0x40] = struct.pack(
                ">8I", 0x3C018000, 0xC4205000, 0x3C018000, 0x24243000, 0x3C018000, 0x24224000, 0x03E00008, 0
            )
            image[0x40:0x46] = b"hello\0"
            image[0x80:0x88] = struct.pack(">II", 0x2018, 0x2018)
            image[0xC0:0xC4] = bytes.fromhex("3f800000")
            slices = [
                dict(start=0x40, end=0x46, address=0x80003000),
                dict(start=0x80, end=0x88, address=0x80004000, table_entry_bias=0x80000000),
                dict(start=0xC0, end=0xC4, address=0x80005000),
            ]
            names = transfer_private(obj, dict(start=0x20, end=0x40, address=0x80002000), bytes(image), slices)
            self.assertEqual(names, [".unbake_pool_80003000", ".unbake_pool_80004000", ".unbake_pool_80005000"])
            rebuilt = Object(obj.path)
            table = rebuilt.section(names[1])
            assert table is not None
            self.assertEqual([offset for offset, _, _ in rebuilt.relocations(table)], [0, 4])
            from unbake.project_tools.rodata import relocated

            self.assertEqual(relocated(rebuilt, names[1], 0x80002000), image[0x80:0x88])
            for row, name in zip(slices, names, strict=True):
                index = rebuilt.section(name)
                assert index is not None
                if name != names[1]:
                    self.assertEqual(rebuilt.content(index), image[row["start"] : row["end"]])

    def test_nonzero_private_tail_needs_compiler_material(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            obj = Object(
                assemble(
                    Path(directory),
                    "alpha",
                    ".set noreorder\n.text\n"
                    "lui $at,%hi(literal)\nlwc1 $f0,%lo(literal)($at)\njr $ra\nnop\n"
                    ".section .rdata\nliteral: .word 0x3f800000\n",
                )
            )
            image = bytearray(0x48)
            image[0x20:0x30] = struct.pack(">4I", 0x3C018000, 0xC4203000, 0x03E00008, 0)
            image[0x40:0x48] = bytes.fromhex("3f80000012345678")
            before = obj.path.read_bytes()
            with self.assertRaisesRegex(ValueError, "unaccounted private"):
                transfer_private(
                    obj,
                    dict(start=0x20, end=0x30, address=0x80002000),
                    bytes(image),
                    [dict(start=0x40, end=0x48, address=0x80003000)],
                )
            self.assertEqual(obj.path.read_bytes(), before)
