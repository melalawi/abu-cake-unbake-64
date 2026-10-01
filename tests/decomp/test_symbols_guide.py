import io
import shutil
import struct
import subprocess
import tempfile
import unittest
from collections.abc import Sequence
from contextlib import redirect_stdout
from dataclasses import replace
from pathlib import Path
from typing import cast

from unbake.decomp import guide, needs, symbols, symbols_edits, trial
from unbake.decomp.needs import LabelNeed, SymbolNeed
from unbake.decomp.trial_artifacts import TrialContext
from unbake.decomp.trial_compare import Compare
from unbake.decomp.trial_layout import FunctionSpan, RomReader
from unbake.decomp.trial_link import inspect
from unbake.families import Family, family_for
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
    )


class SymbolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.family = family_for("ido-7.1")
        root = Path(tempfile.gettempdir())
        self.policy = Policy(
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
            init_same_game_similarity=0.1,
            init_split="functions",
            init_probe_count=1,
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

    def test_address_only_relocations_need_placement_without_type_or_extent(self) -> None:
        for compiler in ("ido-7.1", "gcc-2.7.2-kmc", "gcc-2.8.1-sn64"):
            for addend in (0, 4, -4):
                with self.subTest(compiler=compiler, addend=addend), tempfile.TemporaryDirectory() as temporary:
                    address = 0x800C8000
                    target = pair(address + addend, 9)
                    relocations = (symbols.Relocation(0, 5, "value"), symbols.Relocation(4, 6, "value"))
                    obj = symbols.TrialElf(
                        pair(addend & 0xFFFFFFFF, 9),
                        relocations,
                        (),
                        self.rows,
                        None,
                        family_for(compiler),
                        frozenset(),
                    )
                    derived = symbols.derive(obj, target, "us")
                    inferred = next(n for n in derived if isinstance(n, SymbolNeed))
                    self.assertEqual(
                        (inferred.address, inferred.addend, inferred.type, inferred.size),
                        (address, addend, "address", 0),
                    )
                    self.assertIn("extern u8 value[];", guide.render(derived))
                    self.assertEqual(symbols.symbol_line(inferred), "value = 0x800C8000;")
                    project = project_for(Path(temporary) / "symbols")
                    project.version("us").symbols.write_text("")
                    edit = symbols_edits.resolve(derived, project, self.policy)[0]
                    self.assertEqual(edit.after, "value = 0x800C8000;\n")
                    project.version("us").symbols.write_text("value = 0x800C8000; // type:f32 size:0x4\n")
                    self.assertEqual(symbols_edits.resolve(derived, project, self.policy), [])
                    # Stronger access evidence supersedes the address-only observation,
                    # including when another relocation uses a different addend.
                    typed_target = (*target, *pair(address, 0x31))
                    typed_obj = replace(
                        obj,
                        words=(*obj.words, *pair(0, 0x31)),
                        relocations=(
                            *relocations,
                            symbols.Relocation(8, 5, "value"),
                            symbols.Relocation(12, 6, "value"),
                        ),
                    )
                    typed = next(n for n in symbols.derive(typed_obj, typed_target, "us") if isinstance(n, SymbolNeed))
                    self.assertEqual((typed.type, typed.size), ("f32", 4))
                    for group in ([inferred, typed], [typed, inferred]):
                        self.assertEqual(symbols_edits.resolve(cast(list[needs.Need], group), project, self.policy), [])
                    for size in (-1, 1):
                        with self.assertRaisesRegex(Held, "size"):
                            symbols.symbol_line(replace(inferred, size=size))
                    with self.assertRaisesRegex(Held, "placed-elsewhere"):
                        symbols_edits.resolve([replace(inferred, address=address + 4)], project, self.policy)

    def test_settled_pointer_binding_invalidates_loaded_register(self) -> None:
        address = 0x800FDFCC
        target = (pair(address, 0x23)[0], pair(address, 0x23)[1] ^ (6 << 16), 0xAC400014, 0xAC400088)
        obj = symbols.TrialElf(target, (), (), (), None, self.family, frozenset({address}))
        self.assertEqual([ref.address for ref in symbols.references(target, None)], [address])
        self.assertEqual(symbols.derive(obj, target, "us"), [])

    def test_settled_relocations_need_no_data_row(self) -> None:
        # A configured resident-copy address has no row at its runtime location.
        # Settling the base also covers accesses to its fields through an addend.
        address = 0x800FE2E0
        for compiler in ("ido-7.1", "gcc-2.7.2-kmc", "gcc-2.8.1-sn64"):
            for addend in (0, 4, 8):
                with self.subTest(compiler=compiler, addend=addend):
                    obj = symbols.TrialElf(
                        pair(addend, 0x23),
                        (symbols.Relocation(0, 5, "resident"), symbols.Relocation(4, 6, "resident")),
                        (),
                        (),
                        None,
                        family_for(compiler),
                        frozenset({address, address + addend}),
                    )
                    target = pair(address + addend, 0x23)
                    self.assertEqual(symbols.derive(obj, target, "us"), [])
                    with self.assertRaisesRegex(Held, "data row"):
                        symbols.derive(replace(obj, settled=frozenset()), target, "us")
                    conflicting = symbols.Binding("resident", address + 0x10, ".data", "s32", 4)
                    with self.assertRaisesRegex(Held, "placed-elsewhere"):
                        symbols.derive(replace(obj, bindings=(conflicting,)), target, "us")

    def test_absolute_aggregate_base_and_resident_bin_labels(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project = project_for(root / "symbols")
            version = project.version("us")
            version.split.write_text(
                "segments:\n  - name: main\n    type: code\n    start: 0x40\n"
                "    vram: 0x80200000\n    end: 0x80\n    subsegments:\n"
                "      - [0x40, asm, alpha]\n      - [0x50, bin, globals]\n"
            )
            reader = RomReader(
                version,
                lambda: [dict(address=0x800C0000, start=0x54, end=0x74, table_entry_bias=0)],
            )

            def resident(address: int, size: int) -> symbols.DataRow:
                return guide.resident_row(reader, address, size)

            for addend in (1, 0x5A8, -4):
                with self.subTest(addend=addend):
                    base = 0x801462D4
                    target = (*pair(base + addend), *pair(0x800C0004))
                    obj = symbols.TrialElf(
                        (*pair(addend & 0xFFFFFFFF), *pair(0)),
                        (
                            symbols.Relocation(0, 5, "globals"),
                            symbols.Relocation(4, 6, "globals"),
                            symbols.Relocation(8, 5, "value"),
                            symbols.Relocation(12, 6, "value"),
                        ),
                        (),
                        guide.data_rows(project, "us"),
                        None,
                        self.family,
                        frozenset({base + addend}),
                    )
                    derived = symbols.derive(obj, target, "us", {"field": base + addend}, resident=resident)
                    placed = [n for n in derived if isinstance(n, SymbolNeed)]
                    self.assertEqual(
                        [(n.name, n.address, n.type, n.size, n.section) for n in placed],
                        [("globals", base, "address", 0, "absolute"), ("value", 0x800C0004, "f32", 4, ".data")],
                    )
                    self.assertEqual(
                        [(n.name, n.row) for n in derived if isinstance(n, LabelNeed)], [("value", "globals")]
                    )
                    with self.assertRaisesRegex(Held, "globals: data row"):
                        symbols.derive(obj, target, "us", {}, resident=resident)
                    with self.assertRaisesRegex(Held, "placed-elsewhere"):
                        symbols.derive(
                            replace(obj, bindings=(symbols.Binding("globals", base + 4, ".data", "u8", 1),)),
                            target,
                            "us",
                            {"field": base + addend},
                            resident=resident,
                        )
            self.assertEqual((resident(0x800C0004, 4).start, resident(0x800C0004, 4).end), (0x800C0000, 0x800C0020))
            with self.assertRaisesRegex(Held, "unmapped or ambiguous"):
                resident(0x800C001C, 8)
            ambiguous = replace(obj, rows=(self.rows[0], self.rows[0]))
            with self.assertRaisesRegex(Held, "value: data row"):
                symbols.derive(ambiguous, target, "us", {"field": base - 4}, resident=resident)

    def test_named_function_float_fixtures(self) -> None:
        cases = (
            ("func_80227014", (0x800C7C94, 0x800C7C9C, 0x800C7CA4, 0x800C7CAC)),
            ("func_80243A80", (0x800C8884, 0x800C888C)),
            ("func_8021EED8", (0x800C76EC,)),
        )
        for function, addresses in cases:
            with self.subTest(function=function):
                target: list[int] = []
                draft: list[int] = []
                relocations: list[symbols.Relocation] = []
                for address in addresses:
                    offset = len(target) * 4
                    target.extend(pair(address))
                    draft.extend(pair(0))
                    name = f"D_{address:08X}"
                    relocations.extend((symbols.Relocation(offset, 5, name), symbols.Relocation(offset + 4, 6, name)))
                obj = symbols.TrialElf(tuple(draft), tuple(relocations), (), self.rows, None, self.family, frozenset())
                derived = symbols.derive(obj, target, "us")
                found = [n for n in derived if isinstance(n, SymbolNeed)]
                self.assertEqual([(n.address, n.type, n.size) for n in found], [(a, "f32", 4) for a in addresses])
                self.assertEqual(len([n for n in derived if isinstance(n, LabelNeed)]), len(addresses))
                output = guide.render(derived)
                for address in addresses:
                    self.assertIn(f"extern f32 D_{address:08X};", output)
                    self.assertIn(f"D_{address:08X} = 0x{address:08X}; // type:f32 size:0x4", output)

    def test_signed_low_and_symbol_addends(self) -> None:
        for compiler in ("ido-7.1", "gcc-2.7.2-kmc", "gcc-2.8.1-sn64"):
            family = family_for(compiler)
            self.assertIsInstance(family, Family)
            for address in (0x800C7FFF, 0x800C8000, 0x800CFFFF):
                for addend in (0, 4, -4, 0x8000):
                    with self.subTest(compiler=compiler, address=address, addend=addend):
                        relocations = (symbols.Relocation(0, 5, "hudGlobals"), symbols.Relocation(4, 6, "hudGlobals"))
                        binding = symbols.Binding("hudGlobals", address, ".rodata", "u8", 1)
                        obj = symbols.TrialElf(
                            pair(addend & 0xFFFFFFFF), relocations, (binding,), self.rows, None, family, frozenset()
                        )
                        actual = symbols.derive(obj, pair(address + addend), "us")
                        symbol = next(n for n in actual if isinstance(n, SymbolNeed))
                        self.assertEqual((symbol.name, symbol.address, symbol.addend), ("hudGlobals", address, addend))

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
                actual = guide.from_words(pair(0x800C76EC, opcode), "us", (), self.rows, None, self.family)
                symbol = next(n for n in actual if isinstance(n, SymbolNeed))
                self.assertEqual((symbol.name, symbol.type, symbol.size), ("D_800C76EC", type_, size))
        bindings = (symbols.Binding("hudGlobals", 0x800C76EC, ".rodata", "f32", 4),)
        actual = guide.from_words(pair(0x800C76EC), "us", bindings, self.rows, None, self.family)
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
        hi = symbols.Relocation(0, 5, "a")
        hi2 = symbols.Relocation(4, 5, "a")
        other = symbols.Relocation(8, 5, "b")
        lo = symbols.Relocation(12, 6, "a")
        lo2 = symbols.Relocation(16, 6, "b")
        gp = symbols.Relocation(20, 7, "a")
        for compiler in ("ido-7.1", "gcc-2.7.2-kmc"):
            with self.subTest(compiler=compiler):
                family = family_for(compiler)
                self.assertEqual(
                    family.relocation_pairs((hi, hi2, other, lo, lo2, gp)),
                    [(hi, lo), (hi2, lo), (other, lo2), (None, gp)],
                )
                self.assertEqual(family.relocation_pairs(()), [])
                target = (0x3C02800D, 0x3C02800D, 0xC4448000)
                draft = (0x3C020000, 0x3C020000, 0xC4440000)
                rels = (hi, hi2, symbols.Relocation(8, 6, "a"))
                obj = symbols.TrialElf(draft, rels, (), self.rows, None, family, frozenset())
                inferred = next(n for n in symbols.derive(obj, target, "us") if isinstance(n, SymbolNeed))
                self.assertEqual(inferred.address, 0x800C8000)

    def test_gp_relocation_and_missing_pair_refusals(self) -> None:
        target = (0xC7840004,)
        obj = symbols.TrialElf(
            (0xC7840000,), (symbols.Relocation(0, 7, "value"),), (), self.rows, 0x800C8000, self.family, frozenset()
        )
        found = symbols.derive(obj, target, "us")
        inferred = next(n for n in found if isinstance(n, SymbolNeed))
        self.assertEqual(inferred.address, 0x800C8004)
        cases = (
            (replace(obj, gp=None), target, "value.gp"),
            (replace(obj, relocations=(symbols.Relocation(0, 6, "value"),)), target, "LO16"),
            (
                replace(obj, words=(0x3C020000,), relocations=(symbols.Relocation(0, 5, "value"),)),
                (0x3C02800C,),
                "HI16",
            ),
            (replace(obj, relocations=(symbols.Relocation(2, 7, "value"),)), target, "value.offset"),
            (replace(obj, relocations=(symbols.Relocation(0, 99, "value"),)), target, "unsupported"),
            (replace(obj, rows=()), target, "data row"),
            (replace(obj, bindings=(symbols.Binding("value", 1, ".rodata", "f32", 4),)), target, "placed-elsewhere"),
        )
        for obj, target, reason in cases:
            with self.subTest(reason=reason), self.assertRaisesRegex(Held, reason):
                symbols.derive(obj, target, "us")

    def test_row_boundaries_and_refusals(self) -> None:
        obj = symbols.TrialElf((), (), (), self.rows, None, self.family, frozenset())
        for target, version, reason in (
            (None, "us", "target_words"),
            ((), "", "version"),
            ((-1,), "us", "unsigned word"),
            ((0x100000000,), "us", "unsigned word"),
            (pair(0x800CFFFC, 0x35), "us", "crosses data row"),
        ):
            with self.subTest(reason=reason), self.assertRaisesRegex(Held, reason):
                symbols.derive(obj, cast(Sequence[int], target), version)
        self.assertEqual(symbols.derive(obj, (), "us"), [])
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

    def test_declared_resident_symbols_and_local_pools_need_no_declaration(self) -> None:
        # Absolute or resident symbols already in the symbol file, and the object's own constant
        # section, are placed elsewhere; the trial must still score the draft.
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project = project_for(root / "symbols")
            version = project.version("us")
            version.symbols.write_text("resident = 0x800E0000; // absolute:True\n")
            version.split.write_text(
                "segments:\n  - name: main\n    type: code\n    start: 0x40\n"
                "    vram: 0x800C0000\n    subalign: 4\n    end: 0x50\n"
                "    subsegments:\n      - [0x40, asm, alpha]\n      - [0x48, rodata, pool]\n"
            )
            target = (*pair(0x800E0000), *pair(0x800E0010))
            assembly = root / "alpha.s"
            assembly.write_text(
                ".set noreorder\n.rdata\nlocal: .float 1.0\n.text\n"
                "lui $2, %hi(resident)\nlwc1 $f4, %lo(resident)($2)\n"
                "lui $2, %hi(local)\nlwc1 $f4, %lo(local)($2)\n"
            )
            output = root / "alpha.o"
            subprocess.run(
                [str(self.policy.mips_as), "-EB", "-mips3", "--no-pad-sections", "-o", str(output), str(assembly)],
                check=True,
                capture_output=True,
            )
            unit = inspect(output, str(self.policy.mips_readelf), root)
            proof = trial.Trial("alpha", "0" * 64, {"us": Compare("us", 4, 4, {}, [])}, [], "", [])
            context = TrialContext(
                project,
                self.policy,
                project.src / "alpha.c",
                proof,
                {
                    "us": {
                        "unit": unit,
                        "layout": unit,
                        "span": FunctionSpan(0x800C0000, 0x40, len(target) * 4, 4),
                        "work": root,
                        "target_words": list(target),
                        "version": version,
                    }
                },
            )
            self.assertEqual([n for n in symbols.derive_trial(context) if isinstance(n, SymbolNeed)], [])

    def test_out_of_row_fields_and_undeclared_constants_need_no_declaration(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project = project_for(root / "symbols")
            version = project.version("us")
            version.symbols.write_text("resident = 0x800E0000; // absolute:True\n")
            version.split.write_text(
                "segments:\n  - name: main\n    type: code\n    start: 0x40\n"
                "    vram: 0x800C0000\n    subalign: 4\n    end: 0x50\n"
                "    subsegments:\n      - [0x40, asm, alpha]\n      - [0x48, rodata, pool]\n"
            )
            # The external base is accessed only through +4. The following constant has
            # no declaration; the final access still needs a label inside the data row.
            target = (0x3C03800E, 0x24630000, 0xAC620004, *pair(0x800F0010), *pair(0x800C000C))
            assembly = root / "alpha.s"
            assembly.write_text(
                ".set noreorder\n.text\n"
                "lui $3, %hi(resident)\naddiu $3, $3, %lo(resident)\nsw $2, 4($3)\n"
                "lui $2, 0x800f\nlwc1 $f4, 0x10($2)\n"
                "lui $2, 0x800c\nlwc1 $f4, 0xc($2)\n"
            )
            output = root / "alpha.o"
            subprocess.run(
                [str(self.policy.mips_as), "-EB", "-mips3", "--no-pad-sections", "-o", str(output), str(assembly)],
                check=True,
                capture_output=True,
            )
            unit = inspect(output, str(self.policy.mips_readelf), root)
            proof = trial.Trial("alpha", "0" * 64, {"us": Compare("us", 7, 7, {}, [])}, [], "", [])
            context = TrialContext(
                project,
                self.policy,
                project.src / "alpha.c",
                proof,
                {
                    "us": {
                        "unit": unit,
                        "layout": unit,
                        "span": FunctionSpan(0x800C0000, 0x40, len(target) * 4, 4),
                        "work": root,
                        "target_words": list(target),
                        "version": version,
                    }
                },
            )
            derived = symbols.derive_trial(context)
            self.assertEqual(
                [(n.name, n.address) for n in derived if isinstance(n, SymbolNeed)], [("D_800C000C", 0x800C000C)]
            )
            self.assertEqual([n.name for n in derived if isinstance(n, LabelNeed)], ["D_800C000C"])

    def test_real_elf_deriver_registration_and_guide(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project = project_for(root / "symbols")
            version = project.version("us")
            version.symbols.write_text("")
            version.split.write_text(
                "segments:\n  - name: main\n    type: code\n    start: 0x40\n"
                "    vram: 0x800C0000\n    subalign: 4\n    end: 0x50\n"
                "    subsegments:\n      - [0x40, asm, alpha]\n      - [0x48, rodata, pool]\n"
            )
            target = pair(0x800C000C)
            version.baserom.write_bytes(bytes(0x40) + struct.pack(">II", *target) + bytes(8))
            assembly = root / "alpha.s"
            assembly.write_text(".set noreorder\n.text\nlui $2, %hi(value)\nlwc1 $f4, %lo(value)($2)\n")
            output = root / "alpha.o"
            subprocess.run(
                [str(self.policy.mips_as), "-EB", "-mips3", "--no-pad-sections", "-o", str(output), str(assembly)],
                check=True,
                capture_output=True,
            )
            unit = inspect(output, str(self.policy.mips_readelf), root)
            proof = trial.Trial("alpha", "0" * 64, {"us": Compare("us", 2, 2, {}, [])}, [], "", [])
            context = TrialContext(
                project,
                self.policy,
                project.src / "alpha.c",
                proof,
                {
                    "us": {
                        "unit": unit,
                        "layout": unit,
                        "span": FunctionSpan(0x800C0000, 0x40, len(target) * 4, 4),
                        "work": root,
                        "target_words": list(target),
                        "version": version,
                    }
                },
            )
            derived = symbols.derive_trial(context)
            self.assertEqual(
                [(n.name, n.address) for n in derived if isinstance(n, SymbolNeed)], [("value", 0x800C000C)]
            )
            self.assertIn("extern f32 value;", proof.compares["us"].lines)
            self.assertIn(symbols.derive_trial, needs.derivers())
            self.assertEqual(
                [(kind, order) for kind, order, fn in needs.resolvers() if fn is symbols_edits.resolve],
                [(SymbolNeed, 10), (LabelNeed, 20)],
            )
            with redirect_stdout(io.StringIO()):
                guidance = guide.run(project, "alpha", "us")
            self.assertIn("extern f32 D_800C000C;", guidance)
            for n in derived:
                self.assertEqual(needs.decode(needs.encode(n)), n)

    def test_names_from_correspondence_does_not_require_aligned_relocations(self) -> None:
        cases = (
            ("us", 0x800C0010, 0, "valid", None),
            ("eu", 0x800C0010, 4, "valid", None),
            ("eu", 0x800C0010, -4, "valid", None),
            ("eu", 0x800E0010, 4, "valid", None),
            ("eu", 0x800C0010, 0, "missing", "correspondence"),
            ("eu", 0x800C0010, 0, "ambiguous", "missing or ambiguous"),
            ("eu", 0x800C0010, 0, "unknown", "no aligned evidence"),
        )
        for selected, address, addend, mode, refusal in cases:
            with (
                self.subTest(version=selected, addend=addend, mode=mode, address=address),
                tempfile.TemporaryDirectory() as temporary,
            ):
                root = Path(temporary)
                project = project_for(root / "symbols")
                versions = {
                    name: replace(version, symbols=root / f"{name}.symbols")
                    for name, version in project.version_map.items()
                }
                # An unrelated VERSION lacks a symbol file altogether. Selected
                # VERSIONs must not consult it when resolving correspondence.
                project = replace(project, versions=("us", "eu", "de"), version_map=versions)
                source_address = address - 4
                versions["us"].symbols.write_text(
                    f"left = 0x{source_address - 8:X};\n"
                    + (f"value = 0x{source_address:X};\n" if mode != "unknown" else "")
                    + f"right = 0x{source_address + 8:X};\n"
                )
                versions["eu"].symbols.write_text(
                    f"left = 0x{address - 8:X};\nother = 0x{address:X};\n"
                    + (f"right = 0x{address + 8:X};\n" if mode != "missing" else "")
                    + (f"alias = 0x{address:X};\n" if mode == "ambiguous" else "")
                )
                version = versions[selected]
                version.split.write_text(
                    "segments:\n  - name: main\n    type: code\n    start: 0x40\n"
                    "    vram: 0x800C0000\n    subalign: 4\n    end: 0x60\n"
                    "    subsegments:\n      - [0x40, asm, alpha]\n      - [0x48, rodata, pool]\n"
                )
                assembly = root / "alpha.s"
                assembly.write_text(
                    f".set noreorder\n.text\nlui $3, %hi(value{addend:+d})\nlwc1 $f6, %lo(value{addend:+d})($3)\n"
                )
                output = root / "alpha.o"
                subprocess.run(
                    [
                        str(self.policy.mips_as),
                        "-EB",
                        "-mips3",
                        "--no-pad-sections",
                        "-o",
                        str(output),
                        str(assembly),
                    ],
                    check=True,
                    capture_output=True,
                )
                unit = inspect(output, str(self.policy.mips_readelf), root)
                target_address = source_address if selected == "us" else address
                proof = trial.Trial("alpha", "0" * 64, {selected: Compare(selected, 0, 2, {}, [])}, [], "")
                context = TrialContext(
                    project,
                    self.policy,
                    project.src / "alpha.c",
                    proof,
                    {
                        selected: {
                            "unit": unit,
                            "layout": unit,
                            "work": root,
                            "version": version,
                            "span": FunctionSpan(0x800C0000, 0x40, 8, 4),
                            "target_words": list(pair(target_address + addend)),
                        }
                    },
                )
                if refusal:
                    with self.assertRaisesRegex(Held, f"value:.*{refusal}"):
                        symbols.derive_trial(context)
                else:
                    derived = symbols.derive_trial(context)
                    placed = [n for n in derived if isinstance(n, SymbolNeed) and n.name == "value"]
                    self.assertEqual([n.address for n in placed], [target_address])
                    self.assertEqual(version.symbols.read_text().count("value ="), int(selected == "us"))

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
