"""Instruction families and discriminating probe decisions."""

import struct
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

from tests.project.test_rom import BOOTCODES, cartridge
from unbake.layout import split
from unbake.project import fingerprint, header, rom
from unbake.project.config import Compiler, Held, Project, Version


def move(funct: int, source: Path = 4, destination: int = 2) -> int:
    return source << 21 | destination << 11 | funct


FACTS = header.parse(cartridge(code="EX"), BOOTCODES)


def image(data: bytes) -> bytes:
    return rom.Rom(Path("image.z64"), data, FACTS, "")


def object_bytes(body: bytes, relocations: Any = (), name: str = "probe") -> bytes:
    """Minimal ELF32 MIPS object with real symbols and REL records."""
    strings = b"\0" + name.encode() + b"\0"
    symbols = bytes(16) + struct.pack(">IIIBBH", 1, 0, len(body), 0x12, 0, 1)
    rel = b"".join(struct.pack(">II", offset, (1 << 8) | kind) for offset, kind in relocations)
    text_at, strings_at = 52, 52 + len(body)
    symbols_at = strings_at + len(strings)
    rel_at = symbols_at + len(symbols)
    section_at = rel_at + len(rel)
    sections = [
        (0,) * 10,
        (0, 1, 6, 0, text_at, len(body), 0, 0, 4, 0),
        (0, 3, 0, 0, strings_at, len(strings), 0, 0, 1, 0),
        (0, 2, 0, 0, symbols_at, len(symbols), 2, 1, 4, 16),
        (0, 9, 0, 0, rel_at, len(rel), 3, 1, 4, 8),
    ]
    ident = b"\x7fELF\x01\x02\x01" + bytes(9)
    prefix = struct.pack(">16sHHIIIIIHHHHHH", ident, 1, 8, 1, 0, 0, section_at, 0, 52, 0, 0, 40, 5, 0)
    return prefix + body + strings + symbols + rel + b"".join(struct.pack(">10I", *s) for s in sections)


class FingerprintTests(unittest.TestCase):
    def test_counts_only_register_copies_and_requires_eight_moves_and_eighty_percent(self) -> None:
        words = [move(0x21)] * 8 + [move(0x25)] * 2 + [move(0x21, 0), move(0x21) | 1 << 16, 0x24020021]
        cartridge = image(struct.pack(">" + "I" * len(words), *words))
        counts = fingerprint.idioms(cartridge, [(0, len(cartridge.data))])[0, len(cartridge.data)]
        self.assertEqual(counts, fingerprint.Counts(8, 2))
        self.assertEqual(counts.family, "gcc")
        self.assertIsNone(fingerprint.Counts(7, 0).family)
        self.assertIsNone(fingerprint.Counts(7, 3).family)
        self.assertEqual(fingerprint.Counts(2, 8).family, "ido")
        with self.assertRaises(Held):
            fingerprint.idioms(cartridge, [(1, 8)])

    def test_windows_coalesce_same_family_and_keep_unknown_windows(self) -> None:
        data = struct.pack(">I", move(0x21)) * 0x2000 + struct.pack(">I", move(0x25)) * 0x1000 + bytes(0x4000)
        functions = [
            split.Function("us", f"f_{offset:X}", offset, offset + 0x4000, 0x80000000 + offset, "file", "asm", ())
            for offset in range(0, len(data), 0x4000)
        ]
        regions = fingerprint.regions(functions, image(data))
        self.assertEqual(
            [(r.start, r.end, r.family) for r in regions],
            [(0x80000000, 0x80008000, "gcc"), (0x80008000, 0x8000C000, "ido"), (0x8000C000, 0x80010000, None)],
        )
        self.assertEqual(regions[0].counts.addu, 8192)

    def test_data_in_a_window_cannot_decide_the_function_family(self) -> None:
        data = bytes(0x20) + struct.pack(">I", move(0x25)) * 20
        function = split.Function("us", "entry", 0, 0x20, 0x80000000, "entry", "asm", ())
        self.assertIsNone(fingerprint.regions([function], image(data))[0].family)

    def test_text_clues_never_choose_a_release(self) -> None:
        cartridge = image(b"\0C:\\N64\\INCLUDE\\PSYQ.H\0libultra 2.0\0GCC 2.7.2\0")
        clues = fingerprint.evidence(cartridge)
        self.assertEqual(len(clues), 4)
        self.assertTrue(all("evidence only" in clue for clue in clues))

    def test_release_requires_exclusive_probe_and_no_counterexample(self) -> None:
        probes = ("alpha", "beta")
        self.assertEqual(fingerprint.choose({"a": (True, True), "b": (True, False)}, probes).id, "a")
        for matches in (
            {"a": (True, True), "b": (True, True)},
            {"a": (False, False), "b": (False, False)},
            {"a": (True, False), "b": (False, True)},
        ):
            self.assertIsNone(fingerprint.choose(matches, probes).id)

    def test_failed_candidate_probe_is_not_an_exclusive_match(self) -> None:
        decision = fingerprint.choose(
            {"a": (True, True), "b": (False, True)}, ("failed", "shared"), {"b/failed": "compile failed"}
        )
        self.assertIsNone(decision.id)
        self.assertEqual(decision.scores, {"a": 1, "b": 1})
        self.assertEqual(decision.comparable, ("shared",))
        with self.assertRaisesRegex(Held, "setup.compiler_proposal:.*denominator"):
            fingerprint.choose({"a": (True,)}, ("one", "two"))

    def test_unsorted_generator_and_unknown_leaf_keep_their_own_evidence(self) -> None:
        data = struct.pack(">I", move(0x21)) * 8 + bytes(8) + struct.pack(">I", move(0x21)) * 8
        functions = [
            split.Function("us", name, start, end, 0x80000000 + start, name, "asm", ())
            for name, start, end in (("last", 40, 72), ("first", 0, 32), ("leaf", 32, 40))
        ]
        regions = fingerprint.regions(iter(functions), image(data))
        self.assertEqual([region.family for region in regions], ["gcc", None, "gcc"])
        self.assertEqual([region.functions[0].name for region in regions], ["first", "leaf", "last"])

    def test_probes_need_branch_or_call_and_eight_to_sixty_instructions(self) -> None:
        function = SimpleNamespace(start=0, end=32)
        self.assertFalse(fingerprint.probe(function, struct.pack(">I", move(0x21)) * 8))
        data = struct.pack(">I", 0x0C000000) + bytes(28)
        self.assertTrue(fingerprint.probe(function, data))
        self.assertFalse(fingerprint.probe(SimpleNamespace(start=0, end=28), data))

    def test_relocation_mask_keeps_register_and_instruction_differences(self) -> None:
        expected = struct.pack(">2I", 0x0C001234, 0x3C048000)
        cases = [
            (struct.pack(">2I", 0x0C005678, 0x3C040000), ((0, 4), (4, 5)), True),
            (struct.pack(">2I", 0x0C005678, 0x3C050000), ((0, 4), (4, 5)), False),
            (struct.pack(">2I", 0x08001234, 0x3C048000), ((0, 4),), False),
            (expected + bytes(4), (), False),
        ]
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "probe.o"
            for body, relocations, result in cases:
                with self.subTest(body=body.hex()):
                    path.write_bytes(object_bytes(body, relocations))
                    self.assertEqual(fingerprint.reproduces(path, "probe", expected), result)

    def test_prove_drafts_once_and_preserves_configured_compiler_flags(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "project"
            root.mkdir()
            data = cartridge(instructions=struct.pack(">I", 0x0C000000) + bytes(28))
            baserom = root / "baserom.us.z64"
            baserom.write_bytes(data)
            version = Version("us", baserom, "", root / "split.yaml", root / "symbols.txt", ())
            compilers = {
                ident: Compiler(ident, "ido", root / ident / "cc", root / ident / "as", ("override",), root / "pins")
                for ident in ("first", "second")
            }
            project = Project(
                root=root,
                name="fixture",
                title="Fixture",
                names_from="us",
                versions=("us",),
                src=root / "src",
                include=(root / "include",),
                asm=root / "asm",
                tools=root / "tools",
                compilers=compilers,
                default_compiler="first",
                units={},
                version_map={"us": version},
                id="00000000-0000-4000-8000-000000000001",
                workspace_id="00000000-0000-4000-8000-000000000002",
                roms=root / "roms",
                build=root / "build",
                work=root / "build/work",
                drafts=root / "build/drafts",
            )
            function = split.Function("us", "probe", 0x1000, 0x1020, 0x80001000, "probe", "asm", ())
            region = fingerprint.Region(0x80001000, 0x80001020, "ido", fingerprint.Counts(0, 8), "ido", (function,))
            candidates = [SimpleNamespace(id=ident, cflags=("-O2",)) for ident in compilers]
            calls = []

            def compile_object(selected: Any, policy: Any, source: Path, version: Any, output: Any) -> Any:
                calls.append((selected.default_compiler, selected.compiler_for(source).cflags))
                output.parent.mkdir(parents=True, exist_ok=True)
                body = data[0x1000:0x1020] if selected.default_compiler == "first" else bytes(32)
                output.write_bytes(object_bytes(body))
                return output

            with (
                patch.object(header, "RETAIL", BOOTCODES),
                patch("unbake.project.toolchain.verify"),
                patch("unbake.decomp.m2c.draft", return_value=root.parent / "probe.c") as draft,
                patch("unbake.project.build.compile_object", side_effect=compile_object),
            ):
                decision = fingerprint.prove(
                    project, region, candidates, SimpleNamespace(m2c=Path(sys.executable), probe_count=20)
                )
            self.assertEqual(decision.id, "first")
            self.assertEqual(decision.scores, {"first": 1, "second": 0})
            self.assertEqual(calls, [("first", ("override",)), ("second", ("override",))])
            self.assertEqual(draft.call_count, 1)
