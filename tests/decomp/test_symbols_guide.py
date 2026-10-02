import shutil
import struct
import tempfile
import unittest
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path
from typing import cast

from unbake.decomp import guide, needs, symbols, symbols_edits
from unbake.decomp.needs import LabelNeed, SymbolNeed
from unbake.families import family_for
from unbake.families.mips import Relocation
from unbake.project.config import Compiler, Held, Policy, Project, Version


def pair(address: int, opcode: int = 0x31) -> tuple[int, int]:
    return (0x3C020000 | ((address + 0x8000) >> 16 & 0xFFFF), opcode << 26 | 2 << 21 | 4 << 16 | address & 0xFFFF)


def need(
    name: str = "D_800C7C94", address: int = 0x800C7C94, type_: str = "f32", size: int = 4, version: str = "us"
) -> SymbolNeed:
    return SymbolNeed(version, name, address, 0, ".rodata", type_, size, "fixture")


def mips_tool(name: str) -> Path:
    executable = shutil.which(f"mips-linux-gnu-{name}")
    assert executable is not None
    return Path(executable)


def project_for(symbols_path: Path) -> Project:
    root = symbols_path.parent
    versions = {
        name: Version(name, root / f"baserom.{name}.z64", "0" * 40, root / "game.yaml", symbols_path, ())
        for name in ("us", "eu")
    }
    compiler = Compiler("ido-7.1", "ido", root / "cc", mips_tool("as"), (), root / "compiler.sha256")
    return Project(
        root,
        "fixture",
        "Fixture",
        "us",
        tuple(versions),
        root / "src",
        (root / "include",),
        root / "asm",
        root / "tools",
        {compiler.id: compiler},
        compiler.id,
        {},
        versions,
        id="00000000-0000-4000-8000-000000000001",
        workspace_id="00000000-0000-4000-8000-000000000002",
        roms=root / "roms",
        build=root / "build",
        work=root / "build/work",
        drafts=root / "build/drafts",
    )


class SymbolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.family = family_for("ido-7.1")
        root = Path(tempfile.gettempdir())
        self.policy = Policy(
            setup_version_jobs=1,
            cores=1,
            stall_trials=1,
            search_beam=1,
            assignment_idle_hours=1,
            cache_root=root / "cache",
            state_root=root / "state",
            objdiff_cli=root / "objdiff",
            objdiff_sha256="0" * 64,
            m2c=root / "m2c",
            splat=root / "splat",
            mips_ld=mips_tool("ld"),
            mips_objdump=mips_tool("objdump"),
            mips_readelf=mips_tool("readelf"),
            same_game_similarity=0.1,
            probe_count=1,
            mips_as=mips_tool("as"),
            mips_objcopy=root / "objcopy",
            cpp=root / "cpp",
            asflags=(),
            cppflags=(),
            sn64_asflags=(),
            permuter_archive=root / "permuter.tar",
            permuter_sha256="0" * 64,
        )
        self.rows = (symbols.DataRow("pool", 0x800C0000, 0x800D0000, ".rodata"),)

    def test_constant_pointer_inference_and_load_widths(self) -> None:
        for opcode, type_, size in (
            (0x20, "s8", 1),
            (0x24, "u8", 1),
            (0x21, "s16", 2),
            (0x25, "u16", 2),
            (0x23, "s32", 4),
            (0x31, "f32", 4),
            (0x35, "f64", 8),
            (0x3F, "s64", 8),
        ):
            with self.subTest(opcode=opcode):
                actual = guide.from_words(pair(0x800C76EC, opcode), "us", (), self.rows, None)
                symbol = next(n for n in actual if isinstance(n, SymbolNeed))
                self.assertEqual((symbol.name, symbol.type, symbol.size), ("D_800C76EC", type_, size))
        bindings = (symbols.Binding("hudGlobals", 0x800C76EC, ".rodata", "f32", 4),)
        actual = guide.from_words(pair(0x800C76EC), "us", bindings, self.rows, None)
        self.assertIn("extern f32 hudGlobals;", guide.render(actual))

    def test_decode_endianness_and_tails(self) -> None:
        for order, code in (("big", ">"), ("little", "<")):
            with self.subTest(order=order):
                self.assertEqual(guide.words(struct.pack(code + "II", 0, 0xFFFFFFFF), order), (0, 0xFFFFFFFF))
                self.assertEqual(guide.words(b"", order), ())
        for data, order, reason in ((b"\x00", "big", "target_words"), (b"", "", "byteorder")):
            with self.subTest(reason=reason), self.assertRaisesRegex(Held, reason):
                guide.words(data, order)

    def test_family_pairing_rel_order_and_multiple_highs(self) -> None:
        hi = Relocation(0, 5, "a")
        hi2 = Relocation(4, 5, "a")
        other = Relocation(8, 5, "b")
        lo = Relocation(12, 6, "a")
        lo2 = Relocation(16, 6, "b")
        gp = Relocation(20, 7, "a")
        for compiler in ("ido-7.1", "gcc-2.7.2-kmc"):
            with self.subTest(compiler=compiler):
                family = family_for(compiler)
                self.assertEqual(
                    family.relocation_pairs((hi, hi2, other, lo, lo2, gp)),
                    [(hi, lo), (hi2, lo), (other, lo2), (None, gp)],
                )
                self.assertEqual(family.relocation_pairs(()), [])

    def test_row_boundaries_and_refusals(self) -> None:
        for target, version, reason in (
            (None, "us", "target_words"),
            ((), "", "version"),
            ((-1,), "us", "unsigned word"),
            ((0x100000000,), "us", "unsigned word"),
            (pair(0x800CFFFC, 0x35), "us", "crosses data row"),
        ):
            with self.subTest(reason=reason), self.assertRaisesRegex(Held, reason):
                guide.from_words(cast(Sequence[int], target), version, (), self.rows, None)
        self.assertEqual(guide.from_words((), "us", (), self.rows, None), [])
        self.assertEqual(symbols.references((0x3C02800C, 0x8C420004, 0xC4440000), None)[0].address, 0x800C0004)
        # A load destroys its destination register's constant value.
        self.assertEqual(len(symbols.references((0x3C02800C, 0x8C420004, 0xC4440000), None)), 1)

    def test_delay_slot_and_join_are_not_stale_constants(self) -> None:
        refs = symbols.references((0x3C02800C, 0x10000002, 0xC44476EC, 0xC44476EC), None)
        self.assertEqual([r.offset for r in refs], [8])

    def test_resolver_native_format_and_idempotence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "symbol_addrs.txt"
            path.write_text("hudGlobals = 0x800C76EC; // ignore:false\n")
            project = project_for(path)
            pending: list[needs.Need] = [
                need("hudGlobals", 0x800C76EC),
                need(),
                LabelNeed("us", "D_800C7C94", 0x800C7C94, "pool", "fixture"),
            ]
            edits = symbols_edits.resolve(pending, project, self.policy)
            self.assertEqual(len(edits), 1)
            self.assertIn("ignore:false type:f32 size:0x4", edits[0].after)
            self.assertIn("D_800C7C94 = 0x800C7C94; // type:f32 size:0x4", edits[0].after)
            self.assertNotEqual(path.read_text(), edits[0].after)
            path.write_text(edits[0].after)
            self.assertEqual(symbols_edits.resolve(pending, project, self.policy), [])
            self.assertEqual(symbols_edits.resolve([pending[-1]], project, self.policy), [])

    def test_resolver_conflicts_and_named_refusals(self) -> None:
        cases: tuple[tuple[str, list[needs.Need], str], ...] = (
            ("", [need(), replace(need(), address=0x800C7C98)], "two-addresses"),
            ("D_800C7C94 = 0x800C7C98;\n", [need()], "placed-elsewhere"),
            ("", [replace(need(), address=0x800C7C98)], "address-named"),
            ("D_800C7C94 = 0x800C7C94; // type:s32\n", [need()], "type"),
            ("D_800C7C94 = 0x800C7C94; // size:0x8\n", [need()], "size"),
            ("D_800C7C94 = 0x800C7C94; // size:garbage\n", [need()], "size"),
            ("garbage\n", [need()], "symbol line"),
            ("", [LabelNeed("us", "label", 1, "pool", "fixture")], "LabelNeed"),
            ("", [replace(need(), type="")], "type"),
            ("", [replace(need(), section="")], "section"),
            ("", [replace(need(), size=0)], "size"),
            ("", [replace(need(), name="bad-name")], "name"),
        )
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "symbol_addrs.txt"
            project = project_for(path)
            for before, pending, reason in cases:
                with self.subTest(reason=reason), self.assertRaisesRegex(Held, reason):
                    path.write_text(before)
                    symbols_edits.resolve(pending, project, self.policy)
            with self.assertRaisesRegex(Held, "project.names_from"):
                symbols_edits.resolve([], replace(project, names_from=""), self.policy)
            with self.assertRaisesRegex(Held, "policy"):
                symbols_edits.resolve([], project, cast(Policy, None))
            path.unlink()
            with self.assertRaisesRegex(Held, "symbol_addrs"):
                symbols_edits.resolve([need()], project, self.policy)

    def test_cross_version_address_names_and_shared_file_conflicts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "symbols"
            path.write_text("")
            project = project_for(path)
            self.assertEqual(
                len(symbols_edits.resolve([replace(need(), version="eu", address=0x800C2AD4)], project, self.policy)), 1
            )
            with self.assertRaisesRegex(Held, "two-addresses"):
                symbols_edits.resolve([need(), replace(need(), version="eu", address=0x800C2AD4)], project, self.policy)

    def test_data_rows_explicit_bss_boundaries(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project = project_for(root / "symbols")
            layout = (
                "segments:\n  - name: main\n    type: code\n    start: 0x40\n"
                "    vram: 0x800C0000\n    end: 0x80\n{boundary}    subsegments:\n"
                "      - [0x40, data, values]\n      - [0x50, rodata, pool]\n"
                "      - [0x60, bss, globals]\n"
            )
            for boundary, end in (("    bss_size: 0x20\n", 0x800C0060), ("    bss_end: 0x800C0100\n", 0x800C0100)):
                with self.subTest(boundary=boundary):
                    project.version("us").split.write_text(layout.format(boundary=boundary))
                    rows = guide.data_rows(project, "us")
                    self.assertEqual(
                        [(row.start, row.end) for row in rows],
                        [(0x800C0000, 0x800C0010), (0x800C0010, 0x800C0020), (0x800C0020, end)],
                    )
            project.version("us").split.write_text(layout.format(boundary=""))
            with self.assertRaisesRegex(Held, "main.bss_size"):
                guide.data_rows(project, "us")

    def test_exact_extern_extent_and_prologue(self) -> None:
        for type_, size, expected in (
            ("f32", 4, "extern f32 value;"),
            ("u8", 3, "extern u8 value[3];"),
            ("s16", 8, "extern s16 value[4];"),
            ("f64", 16, "extern f64 value[2];"),
        ):
            with self.subTest(type=type_, size=size):
                self.assertEqual(guide.extern(need("value", type_=type_, size=size)), expected)
        for type_, size, reason in (("f32", 3, "size"), ("pointer", 4, "type")):
            with self.subTest(reason=reason), self.assertRaisesRegex(Held, reason):
                guide.extern(need("value", type_=type_, size=size))
        self.assertEqual(
            guide.prologue((0x27BDFFD8, 0xAFBF0024, 0xAFB00020)),
            "frame: 0x28 bytes\nsaved: ra at sp+36\nsaved: s0 at sp+32",
        )


if __name__ == "__main__":
    unittest.main()
