"""Compiler and assembly providers share exact ROM storage without duplicates."""

import os
import struct
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.decomp.support import assemble
from unbake.objects.elf import Object
from unbake.project_tools.layout import place, transfer_private
from unbake.project_tools.pool_slices import Provider, link_pools
from unbake.objects.rodata import relocated


class PoolOwnershipTests(unittest.TestCase):
    def fixtures(self, root, material=b"hello\0TAIL", compilers=(("alpha", b"hello\0", 0),), pool_section=".rodata"):
        row = dict(
            start=0x40,
            end=0x40 + len(material),
            address=0x80003000,
            path="rodata/shared/80003000",
            section=pool_section,
        )
        image = bytes(0x40) + material
        pool = "obj/asm/data/" + row["path"] + pool_section + ".o"
        path = root / pool
        path.parent.mkdir(parents=True)
        assemble(path.parent, path.stem, ".section " + pool_section + "\n.byte " + ",".join(map(str, material)))
        script = "SECTIONS { .pool : SUBALIGN(1) { " + pool + "(" + pool_section + "); } }"
        providers = []
        for name, data, offset in compilers:
            directory = root / "obj/src"
            directory.mkdir(parents=True, exist_ok=True)
            section = f".unbake_pool_{row['address'] + offset:08X}"
            assemble(
                directory,
                name,
                ".set noreorder\n.text\nlui $at,%hi(literal)\naddiu $a0,$at,%lo(literal)\njr $ra\nnop\n"
                f".section {section}\nliteral: .byte " + ",".join(map(str, data)),
            )
            providers.append(Provider(f"obj/src/{name}.o", section, row["address"] + offset, 0x80002000))
        return row, image, script, providers, path

    def selected_bytes(self, root, script):
        import re

        result = bytearray()
        for name, section in re.findall(r"(obj/[^\s();]+)\(([^)]+)\)", script):
            obj = Object(root / name)
            result.extend(obj.content(obj.section(section)))
        return bytes(result)

    def test_provider_table(self):
        cases = (
            ("private with asm tail", b"hello\0TAIL", (("alpha", b"hello\0", 0),), 1),
            ("all compiler", b"hello\0", (("alpha", b"hello\0", 0),), 0),
            ("two published owners", b"hello\0TAIL", (("alpha", b"hello\0", 0), ("beta", b"TAIL", 6)), 0),
            ("duplicate published string", b"hello\0TAIL", (("beta", b"hello\0", 0), ("alpha", b"hello\0", 0)), 1),
            ("overlapping suffix", b"hello\0TAIL", (("alpha", b"hello\0", 0), ("beta", b"lo\0", 3)), 1),
            ("asm prefix and tail", b"Phello\0TAIL", (("alpha", b"hello\0", 1),), 2),
            ("zero is compiler storage", b"\0TAIL", (("alpha", b"\0", 0),), 1),
        )
        for label, material, compilers, remainders in cases:
            with self.subTest(label=label), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                row, image, script, providers, path = self.fixtures(root, material, compilers)
                before = path.read_bytes()
                shared = path.with_suffix(".shared")
                os.link(path, shared)
                result = link_pools(root, script, [row], providers, image)
                self.assertEqual(self.selected_bytes(root, result), material)
                self.assertEqual(result.count(".unbake-pool.o("), remainders)
                self.assertEqual(path.read_bytes(), before)
                self.assertEqual(shared.read_bytes(), before)
                for p in providers:
                    obj = Object(root / p.object)
                    text = obj.section(".text")
                    self.assertTrue(all(s["section"] == 0xFFF1 for _, _, s in obj.relocations(text)))
                    self.assertTrue(all(s["info"] & 15 != 3 for _, _, s in obj.relocations(text)))
                    self.assertEqual({s["value"] for _, _, s in obj.relocations(text)}, {p.address})
                snapshots = {p.object: (root / p.object).read_bytes() for p in providers}
                self.assertEqual(link_pools(root, script, [row], list(reversed(providers)), image), result)
                self.assertEqual({n: (root / n).read_bytes() for n in snapshots}, snapshots)

    def test_disagreements_and_selector_failures_publish_nothing(self):
        cases = (
            ("compiler", "0x80003002"),
            ("assembly", "assembly bytes disagree"),
            ("selector", "expected one load selector"),
            ("unmapped", "not in one structural"),
        )
        for fault, message in cases:
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                data = b"heXlo\0" if fault == "compiler" else b"hello\0"
                row, image, script, providers, path = self.fixtures(root, compilers=(("alpha", data, 0),))
                if fault == "assembly":
                    assemble(path.parent, path.stem, ".section .rodata\n.byte 0")
                if fault == "selector":
                    script += script
                if fault == "unmapped":
                    providers = [Provider(providers[0].object, providers[0].section, 0x80004000, 0x80002000)]
                snapshots = {p: p.read_bytes() for p in root.rglob("*.o")}
                with patch("unbake.project_tools.pool_slices.write") as publish:
                    with self.assertRaisesRegex(ValueError, message):
                        link_pools(root, script, [row], providers, image)
                    publish.assert_not_called()
                self.assertEqual({p: p.read_bytes() for p in root.rglob("*.o")}, snapshots)

    def test_private_external_constants_remain_assembly(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            obj = Object(assemble(root, "alpha", ".text\nlui $at,%hi(external)\naddiu $a0,$at,%lo(external)"))
            before = obj.path.read_bytes()
            self.assertEqual(
                transfer_private(
                    obj,
                    dict(start=0, end=8, address=0x80002000),
                    bytes(0x44),
                    [dict(start=0x40, end=0x44, address=0x80003000)],
                ),
                [],
            )
            self.assertEqual(obj.path.read_bytes(), before)

    def test_transfer_failure_in_second_section_is_atomic(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            obj = Object(
                assemble(
                    root,
                    "alpha",
                    ".set noreorder\n.text\n"
                    "lui $at,%hi(literal)\nlwc1 $f0,%lo(literal)($at)\n"
                    "lui $at,%hi(string)\naddiu $a0,$at,%lo(string)\n"
                    ".section .rdata\nliteral: .word 0x3f800000\n"
                    '.section .rodata\nstring: .asciz "wrong"',
                )
            )
            image = bytearray(0x86)
            image[:16] = struct.pack(">4I", 0x3C018000, 0xC4203000, 0x3C018000, 0x24244000)
            image[0x40:0x44] = bytes.fromhex("3f800000")
            image[0x80:0x86] = b"hello\0"
            before = obj.path.read_bytes()
            with patch("unbake.project_tools.layout.write") as publish:
                with self.assertRaisesRegex(ValueError, "disagree at 0x80004000"):
                    transfer_private(
                        obj,
                        dict(start=0, end=16, address=0x80002000),
                        bytes(image),
                        [
                            dict(start=0x40, end=0x44, address=0x80003000),
                            dict(start=0x80, end=0x86, address=0x80004000),
                        ],
                    )
                publish.assert_not_called()
            self.assertEqual(obj.path.read_bytes(), before)
            self.assertEqual(bytes(obj.data), before)

    def test_shared_table_keeps_relocations_and_bias(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            obj = Object(
                assemble(
                    root,
                    "table",
                    ".set noreorder\n.text\n"
                    "lui $at,%hi(table)\naddiu $v0,$at,%lo(table)\ncase: jr $ra\nnop\n"
                    ".section .rodata\ntable: .word case,case",
                )
            )
            image = bytearray(0x4C)
            image[:16] = struct.pack(">4I", 0x3C018000, 0x24223000, 0x03E00008, 0)
            image[0x40:0x4C] = struct.pack(">3I", 0x2008, 0x2008, 0x12345678)
            row = dict(
                start=0x40, end=0x4C, address=0x80003000, table_entry_bias=0x80000000, path="rodata/shared/80003000"
            )
            names = transfer_private(obj, dict(start=0, end=16, address=0x80002000), bytes(image), [row])
            self.assertEqual(relocated(obj, names[0], 0x80002000), image[0x40:0x48])
            source = root / "obj/src/table.o"
            source.parent.mkdir(parents=True)
            source.write_bytes(obj.path.read_bytes())
            pool = root / "obj/asm/data/rodata/shared/80003000.rodata.o"
            pool.parent.mkdir(parents=True)
            assemble(pool.parent, pool.stem, ".section .rodata\n.word 0x2008,0x2008,0x12345678")
            script = link_pools(
                root,
                "obj/asm/data/rodata/shared/80003000.rodata.o(.rodata)",
                [row],
                [Provider("obj/src/table.o", names[0], 0x80003000, 0x80002000)],
                bytes(image),
            )
            rebuilt = Object(source)
            section = ".unbake_piece_80003000_8"
            self.assertEqual(relocated(rebuilt, section, 0x80002000), image[0x40:0x48])
            self.assertEqual([at for at, _, _ in rebuilt.relocations(rebuilt.section(section))], [0, 4])
            self.assertIn(".unbake-pool.o", script)

    def test_place_uses_global_pool_ownership_once(self):
        import json
        from argparse import Namespace

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            row, image, script, _, _ = self.fixtures(root)
            (root / "unit-ranges.json").write_text(json.dumps({"alpha": dict(start=0, end=16, address=0x80002000)}))
            (root / "pool-providers.json").write_text(json.dumps([row]))
            (root / "rom").write_bytes(image)
            (root / "script").write_text(script + "\nobj/src/alpha.o(.text)\n/DISCARD/ : { *(*) }")
            args = Namespace(
                script=root / "script",
                ranges=root / "unit-ranges.json",
                baserom=root / "rom",
                recipe=None,
                build=root,
                non_matching="0",
                output=root / "out",
            )
            with patch("unbake.project_tools.layout.link_pools", return_value="final") as link:
                place(args)
                self.assertEqual(link.call_count, 1)
                self.assertEqual(link.call_args.args[2][0]["path"], row["path"])
            self.assertEqual((root / "out").read_text(), "final")

    def test_structural_data_providers_and_assembly_remainders(self):
        for section in (".data", ".rodata"):
            with self.subTest(section=section), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                row, image, script, providers, path = self.fixtures(root, pool_section=section)
                original = path.read_bytes()
                result = link_pools(root, script, [row], providers, image)
                self.assertEqual(self.selected_bytes(root, result), b"hello\0TAIL")
                self.assertIn(section + ".unbake-pool.o", result)
                self.assertEqual(path.read_bytes(), original)

    def test_explicit_storage_crosses_structural_slice_boundaries(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            obj = Object(
                assemble(
                    root, "alpha", ".text\njr $ra\nnop\n.section .rdata\nunbake_rodata_80003001_7: .byte 1,2,3,4,5,6,7"
                )
            )
            image = bytes(0x40) + bytes(range(8))
            rows = [dict(start=0x40, end=0x44, address=0x80003000), dict(start=0x44, end=0x48, address=0x80003004)]
            names = transfer_private(obj, dict(start=0, end=8, address=0x80002000), image, rows)
            self.assertEqual(names, [".unbake_pool_80003001", ".unbake_pool_80003004"])
            self.assertEqual(obj.content(obj.section(names[0])), bytes((1, 2, 3)))
            self.assertEqual(obj.content(obj.section(names[1])), bytes((4, 5, 6, 7)))

    def test_pool_catalogue_includes_structural_data_anchors(self):
        from unbake.project_tools.extract import pool_rows

        yaml = (
            "segments:\n  - name: main\n    type: code\n    start: 0x20\n    vram: 0x80002000\n"
            "    subsegments:\n      - [0x20, c, alpha]\n"
            '      - [0x40, data, "rodata/unresolved/80002020"]\n'
            '      - [0x48, rodata, "rodata/shared/80002028"]\n  - [0x4C]\n'
        )
        rows = pool_rows(yaml, storage=True)
        self.assertEqual([r["section"] for r in rows], [".data", ".rodata"])
        self.assertEqual([r["owner"] for r in rows], [None, None])

    def test_stale_piece_disagreement_is_named_before_publication(self):
        from unbake.objects.literal_layout import replace
        from unbake.project_tools.pool_slices import piece

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, _, _, providers, _ = self.fixtures(root)
            obj = Object(root / providers[0].object)
            index = obj.section(providers[0].section)
            name = piece(obj, index, 0, 6, 0x80003000)
            replace(obj, obj.section(name), b"wrong\0")
            with self.assertRaisesRegex(ValueError, "existing provider disagrees at 0x80003000"):
                piece(obj, index, 0, 6, 0x80003000)

    def test_two_compiler_sections_supply_one_structural_pool(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            material = bytes.fromhex("3f800000") + b"hello\0TAIL"
            row, image, script, _, _ = self.fixtures(root, material, compilers=())
            source = root / "obj/src/alpha.o"
            source.parent.mkdir(parents=True)
            obj = Object(
                assemble(
                    source.parent,
                    source.stem,
                    ".set noreorder\n.text\n"
                    "lui $at,%hi(literal)\nlwc1 $f0,%lo(literal)($at)\n"
                    "lui $at,%hi(string)\naddiu $a0,$at,%lo(string)\n"
                    ".section .rdata\nliteral: .word 0x3f800000\n"
                    '.section .rodata\nstring: .asciz "hello"',
                )
            )
            rom = bytearray(image)
            rom[:16] = struct.pack(">4I", 0x3C018000, 0xC4203000, 0x3C018000, 0x24243004)
            names = transfer_private(obj, dict(start=0, end=16, address=0x80002000), bytes(rom), [row])
            self.assertEqual(names, [".unbake_pool_80003000", ".unbake_pool_80003004"])
            providers = [
                Provider("obj/src/alpha.o", name, int(name.rsplit("_", 1)[1], 16), 0x80002000) for name in names
            ]
            result = link_pools(root, script, [row], providers, bytes(rom))
            self.assertEqual(self.selected_bytes(root, result), material)
            self.assertEqual(result.count(".unbake-pool.o("), 1)
