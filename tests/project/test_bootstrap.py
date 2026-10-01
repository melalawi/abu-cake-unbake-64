"""Bootstrap integration with real records and focused external-tool boundaries."""

import hashlib
import shutil
import struct
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest.mock import patch

from unbake.decomp import m2c
from unbake.layout import split
from unbake.project import build, config, fingerprint, header, init, rom, toolchain
from unbake.project.config import Held
from unbake.report import progress as report
from unbake.report import units as report_units


def policy(root: Path) -> Any:
    executable = Path(sys.executable)
    return config.Policy(
        cores=2,
        stall_trials=3,
        search_beam=2,
        assignment_idle_hours=3,
        cache_root=root / "cache",
        state_root=root / "state",
        objdiff_cli=executable,
        objdiff_sha256="0" * 64,
        m2c=executable,
        splat=executable,
        mips_ld=executable,
        mips_objdump=executable,
        mips_readelf=executable,
        init_same_game_similarity=0.1,
        init_split="functions",
        init_probe_count=4,
        mips_as=executable,
        mips_objcopy=executable,
        cpp=executable,
        asflags=("-EB",),
        cppflags=(),
        sn64_asflags=(),
        permuter_archive=root / "permuter.tar",
        permuter_sha256="0" * 64,
    )


def cartridge(path: Any, data: bytes) -> Any:
    facts = header.Header(0x80371240, 15, 0x80000000, 0x1444, 20, "D", 0, 0, "Example", "N", "EX", "E", 0, "6102/7101")
    return rom.Rom(path, data, facts, hashlib.sha1(data).hexdigest())


def function(name: str, start: int, end: int, address: int) -> Any:
    return split.Function("us", name, start, end, address, name, "asm", ())


class BootstrapTests(unittest.TestCase):
    def test_function_family_boundaries_and_ambiguous_copies(self) -> None:
        addresses = (0x80110480, 0x80110490, 0x80124340, 0x80124350)
        for mixed in (False, True):
            with self.subTest(mixed=mixed):
                words = [0x00801021, 0x00801025, 0x00801025, 0x00801021]
                if mixed:
                    words[1] = 0x00801021
                data = b"".join(struct.pack(">I", word) * 4 for word in words)
                functions = [
                    function(str(address), index * 16, (index + 1) * 16, address)
                    for index, address in enumerate(addresses)
                ]
                regions = fingerprint.regions(functions, cartridge(Path("image"), data))
                selected = [region for region in regions if region.family == "ido"]
                self.assertEqual(
                    (selected[0].start, selected[0].end), (0x80124340 if mixed else 0x80110490, 0x80124350)
                )
        data = bytes.fromhex("0080102100801025")
        regions = fingerprint.regions([function("mixed", 0, 8, 0x80000000)], cartridge(Path("image"), data))
        self.assertIsNone(regions[0].family)

    def test_similarity_uses_measured_code_and_symmetric_rom_pairs(self) -> None:
        code = bytes.fromhex("27bdffe8afbf0014008010210c000004000000008fbf001403e0000827bd0018")
        changed = bytes.fromhex("3c0280003442000124420001ac8200008c820000104000010000000003e00008")
        for other_code, asset, accepted in ((code, b"different", True), (changed, b"asset", False)):
            with self.subTest(accepted=accepted):
                a = cartridge(Path("a"), code + b"asset")
                b = cartridge(Path("b"), other_code + asset)
                inventories = {item.path: [function("f", 0, 32, 0x80000000)] for item in (a, b)}
                if accepted:
                    matrix = rom.same_game([a, b], inventories, 0.9)
                    self.assertEqual((matrix[a, b], matrix[b, a], matrix[a, a]), (1.0, 1.0, 1.0))
                else:
                    with self.assertRaisesRegex(Held, "code similarity"):
                        rom.same_game([a, b], inventories, 0.9)
        for inventories, threshold, name in (
            ({}, 0.5, "detected code ranges"),
            ({a.path: []}, 0.5, "detected code ranges"),
            ({}, False, "init_same_game_similarity"),
        ):
            with self.subTest(name=name), self.assertRaisesRegex(Held, name):
                rom.same_game([a], inventories, threshold)

    def test_naming_version_refusals(self) -> None:
        cases = (
            (("us",), None, "us"),
            (("us", "eu"), "eu", "eu"),
            (("us", "eu"), None, "--names-from"),
            (("us",), "jp", "unknown VERSION"),
            ((), None, "project.versions"),
        )
        for versions, selected, expected in cases:
            with self.subTest(versions=versions, selected=selected):
                if expected in versions:
                    self.assertEqual(init.naming_version(versions, selected), expected)
                else:
                    with self.assertRaisesRegex(Held, expected):
                        init.naming_version(versions, selected)

    def test_probe_preserves_project_flags_and_header_context(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "project"
            shutil.copytree(Path(__file__).parents[1] / "fixture", root)
            project = config.load(root)
            original = project.compilers[project.default_compiler]
            compilers = {
                name: replace(original, id=name, cflags=("-O1", "-G0")) for name in ("gcc-2.7.2-kmc", "gcc-2.8.1-sn64")
            }
            project = replace(
                project, compilers=compilers, default_compiler="gcc-2.7.2-kmc", units={"existing": "gcc-2.7.2-kmc"}
            )
            candidates = [toolchain.registry()[name] for name in compilers]
            body = bytes.fromhex("27bdffe8afbf0014008010210c000004000000008fbf001403e0000827bd0018")
            probe = function("probe", 0, 32, 0x80000000)
            region = fingerprint.Region(
                probe.address, probe.address + 32, "gcc", fingerprint.Counts(1, 0), "main", (probe,)
            )
            calls = []

            def compile_object(selected: Any, policy: Any, source: Path, version: Any, out: Any) -> Any:
                calls.append(
                    (selected.default_compiler, selected.compiler_for("probe").cflags, selected.include, selected.units)
                )
                out.parent.mkdir(parents=True, exist_ok=True)
                out.write_bytes(
                    report.target_object("probe", body if selected.default_compiler == candidates[0].id else bytes(32))
                )
                return out

            def draft(selected: Any, policy: Any, name: str, version: Any, work: Any) -> Any:
                source = work / "probe.c"
                source.write_text('#include "types.h"\nint probe(void) { return 0; }\n')
                return source

            with (
                patch.object(build, "compile_object", side_effect=compile_object, autospec=True),
                patch.object(m2c, "draft", side_effect=draft, autospec=True),
                patch.object(toolchain, "verify", autospec=True),
                patch.object(rom, "load", return_value=cartridge(project.version("us").baserom, body), autospec=True),
            ):
                decision = fingerprint.prove(project, region, candidates, policy(root))
            self.assertEqual(decision.id, candidates[0].id)
            self.assertEqual([call[:2] for call in calls], [(c.id, ("-O1", "-G0")) for c in candidates])
            self.assertTrue(all(call[2][:-1] == project.include for call in calls))
            self.assertEqual(
                [call[3] for call in calls], [{"existing": candidates[0].id, "probe": c.id} for c in candidates]
            )

    def test_report_uses_existing_partial_object_without_compiling(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "project"
            shutil.copytree(Path(__file__).parents[1] / "fixture", root)
            project = config.load(root)
            source = project.src / "alpha.c"
            project.version("us").baserom.write_bytes(bytes.fromhex("2402000103e0000800000000"))
            project.version("us").split.write_text(
                "segments:\n  - name: main\n    type: code\n    start: 0\n    vram: 0x"
                "80000000\n    subsegments:\n      - [0, asm, alpha]\n  - [12]\n"
            )
            source.write_text("#ifdef NON_MATCHING\nint alpha(void) { return 1; }\n#endif\n")
            partial = root / "build/us.nonmatching/obj/src/alpha.o"
            partial.parent.mkdir(parents=True)
            partial.write_bytes(report.target_object("alpha", bytes.fromhex("2402000103e0000800000000")))
            target = root / "generation/obj/asm/alpha.o"
            target.parent.mkdir(parents=True)
            target.write_bytes(partial.read_bytes())
            with patch.object(build, "compile_object", autospec=True) as compile_object:
                units = report_units.units(project, policy(root), "us", root / "generation", root / "state")
            compile_object.assert_not_called()
            unit = next(unit for unit in units if unit["name"] == "alpha")
            self.assertFalse(unit["metadata"]["complete"])
            self.assertEqual((root / "generation" / unit["base_path"]).resolve(), partial.resolve())

    def test_report_refuses_missing_values_by_name(self) -> None:
        fields = ("matched_code", "total_code", "matched_code_percent", "fuzzy_match_percent")
        for field in fields:
            for bad in (None, True, -1):
                with self.subTest(field=field, bad=bad):
                    measures = dict.fromkeys(fields, 0)
                    measures[field] = bad
                    with self.assertRaisesRegex(Held, field):
                        report.progress({"us": {"version": 2, "measures": measures}}, {"us": "us (test)"})
        for template, name in (
            ("", "readme.Progress"),
            ("## Progress\n\n", "following section"),
            ("## Progress\n\nold\n## End\n", "descriptions.us"),
        ):
            with self.subTest(name=name), self.assertRaisesRegex(Held, name):
                report.render(template, {"us": {"version": 2, "measures": dict.fromkeys(fields, 0)}})
