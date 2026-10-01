"""Read-only ownership and all-VERSION private migration command regressions."""

import argparse
import contextlib
import io
import json
import struct
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import patch

from tests.decomp.support import assemble
from tests.layout.test_split import ProjectFixture
from unbake.cli import rodata
from unbake.layout import split
from unbake.layout.rodata_bulk import Group, hosts, migrate_all
from unbake.layout.rodata_migrate import migrate
from unbake.project.config import Held, Policy, Project
from unbake.project_tools.elf import Object
from unbake.project_tools.layout import resident
from unbake.project_tools.literal_layout import arrange
from unbake.project_tools.rodata import relocated


class RodataCommandTests(unittest.TestCase):
    def test_shared_owners_use_their_common_file(self) -> None:
        from unbake.layout.rodata_owners import Constant, scan

        census = scan(self.project, "us")
        census.functions = [
            split.Function("us", name, 0x10, 0x20, 0x80001000, "common", "c", ()) for name in ("alpha", "beta")
        ]
        (self.fixture.src / "common.c").write_text("void alpha(void) {}\nvoid beta(void) {}\n")
        build = self.fixture.build_link("us")
        (build / "obj/src").mkdir()
        (build / "obj/src/common.o").write_bytes((build / "obj/asm/alpha.o").read_bytes())
        row = next(
            row
            for segment in split.layout(self.fixture.version("us").split)[2]
            for row in segment.rows
            if row.kind == "bin"
        )
        item = Constant(0x80003000, 0x80003004, "float", "resident", "load", [], owners={"alpha", "beta"})
        group = Group(0x40, 0x44, item.address, row, [item])
        hosts(self.project, census, [group])
        self.assertEqual(group.host, "common")

    def test_bulk_includes_shared_storage_and_preserves_access_expressions(self) -> None:
        original = self.source.read_text()
        (self.fixture.src / "beta.c").write_text("extern float D_SHARED;\nfloat beta(void) { return D_SHARED; }\n")
        for version in self.fixture.versions:
            build = self.fixture.build_link(version)
            (build / "obj/src").mkdir()
            for name in ("alpha", "beta"):
                (build / "obj/src" / (name + ".o")).write_bytes((build / "obj/asm" / (name + ".o")).read_bytes())
        bulk = migrate_all(self.project)
        for version in self.fixture.versions:
            self.assertEqual(len(bulk.migrated[version]), 2)
            self.assertEqual(bulk.migrated[version][0]["owners"], ["alpha", "beta"])
            self.assertFalse(bulk.resident[version])
        alpha = next(edit.after for edit in bulk.edits if edit.path == self.source)
        self.assertTrue(alpha.startswith(original))
        self.assertIn("const float unbake_rodata_", alpha)
        for edit in bulk.edits:
            if edit.path.suffix == ".c":
                subprocess.run(
                    ["cc", "-std=c89", "-pedantic-errors", "-DVERSION_US", "-x", "c", "-fsyntax-only", "-"],
                    input=edit.after.encode(),
                    check=True,
                )
        self.assertEqual(self.source.read_text(), original)

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.fixture = ProjectFixture(self.root)
        self.project = cast(Project, self.fixture)
        self.fixture.src.mkdir()
        self.source = self.fixture.src / "alpha.c"
        self.source.write_text(
            "extern float D_SHARED;\nextern float D_PRIVATE;\nfloat alpha(void) { return D_SHARED + D_PRIVATE; }\n"
        )
        self.mappings: dict[str, list[dict[str, int]]] = {}
        for index, version in enumerate(self.fixture.versions):
            configured = self.fixture.version(version)
            configured.macros = ("VERSION_" + version.upper(),)
            self.fixture.layout(version, [(0x10, "c", "alpha"), (0x20, "c", "beta"), (0x40, "bin", "pool")])
            runtime = 0x80003000 + index * 0x100
            code = [0x3C018000, 0xC4200000 | (runtime & 0xFFFF), 0x3C018000, 0xC4220000 | ((runtime + 4) & 0xFFFF)]
            image = bytearray(128)
            image[0x10:0x20] = struct.pack(">4I", *code)
            image[0x20:0x28] = struct.pack(">2I", *code[:2])
            image[0x40:0x48] = struct.pack(">2f", 1.0, float(index + 2))
            configured.baserom.write_bytes(image)
            configured.symbols.write_text(
                f"alpha = 0x80001000;\nbeta = 0x80001010;\nD_SHARED = 0x{runtime:X};\nD_PRIVATE = 0x{runtime + 4:X};\n"
            )
            build = self.fixture.build_link(version)
            (build / "obj/asm").mkdir(parents=True)
            for name, loads in (("alpha", 2), ("beta", 1)):
                asm = ".set noreorder\n.text\n.globl " + name + "\n" + name + ":\n"
                for load in range(loads):
                    asm += f"lui $at,%hi(D_{load})\nlwc1 $f{load * 2},%lo(D_{load})($at)\n"
                obj = assemble(build, name, asm)
                (build / "obj/asm" / (name + ".o")).write_bytes(obj.read_bytes())
            linked = assemble(
                build,
                "linked",
                f".text\n.globl D_SHARED\n.equ D_SHARED,0x{runtime:X}\n",
            )
            (build / "fixture.elf").write_bytes(linked.read_bytes())
            (build / "objdiff.json").write_text(json.dumps({"units": []}))
            self.mappings[version] = [{"address": runtime, "start": 0x40, "end": 0x60, "table_entry_bias": 0}]
        facts = SimpleNamespace(resident_mappings=self.mappings)
        for name in ("unbake.layout.rodata_owners.recipe", "unbake.project.makefile.recipe"):
            started = patch(name, return_value=facts)
            started.start()
            self.addCleanup(started.stop)

    def test_owners_command_preserves_inputs_and_distinguishes_shared(self) -> None:
        before = {p: p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            held = rodata.run(argparse.Namespace(verb="owners", version="us"), self.project, cast(Policy, None))
        self.assertFalse(held)
        result = json.loads(output.getvalue())
        typed = [item for item in result["objects"] if item["kind"] == "float"]
        self.assertEqual([item["owners"] for item in typed], [["alpha", "beta"], ["alpha"]])
        self.assertEqual([item["safe_sole_candidate"] for item in typed], [False, True])
        self.assertEqual([item["names"] for item in typed], [["D_SHARED"], ["D_PRIVATE"]])
        self.assertEqual(before, {p: p.read_bytes() for p in self.root.rglob("*") if p.is_file()})

    def test_migrate_command_stages_native_splits_for_real_version_differences(self) -> None:
        original = self.source.read_text()
        for declaration, expression in (
            ("extern float D_PRIVATE;", "(D_SHARED && D_PRIVATE) ? D_PRIVATE : 0.0f"),
            ("typedef struct { float value; } Private;\nextern Private D_PRIVATE;", "D_SHARED + D_PRIVATE.value"),
        ):
            with self.subTest(expression=expression):
                self.source.write_text(
                    "extern float D_SHARED;\n" + declaration + "\nfloat alpha(void) { return " + expression + "; }\n"
                )
                rewritten = next(e.after for e in migrate(self.project, "alpha") if e.path == self.source)
                subprocess.run(
                    ["cc", "-std=c89", "-pedantic-errors", "-DVERSION_US", "-x", "c", "-fsyntax-only", "-"],
                    input=rewritten.encode(),
                    check=True,
                )
        for version in self.fixture.versions:
            runtime = self.mappings[version][0]["address"]
            symbols = self.fixture.version(version).symbols
            symbols.write_text(symbols.read_text() + f"D_BASE = 0x{runtime:X};\n")
            build = self.fixture.build_link(version)
            offset_object = assemble(
                build,
                "offset_alpha",
                ".set noreorder\n.text\n.globl alpha\nalpha:\n"
                "lui $at,%hi(D_SHARED)\nlwc1 $f0,%lo(D_SHARED)($at)\n"
                "lui $at,%hi(D_BASE+4)\nlwc1 $f2,%lo(D_BASE+4)($at)\n",
            )
            (build / "obj/src").mkdir()
            (build / "obj/src/alpha.o").write_bytes(offset_object.read_bytes())
        self.source.write_text(
            "typedef struct { char padding[4]; float value; } Private;\n"
            "extern float D_SHARED;\nextern float D_BASE;\n"
            "float alpha(void) { return D_SHARED + ((Private *)(&D_BASE))->value; }\n"
        )
        rewritten = next(e.after for e in migrate(self.project, "alpha") if e.path == self.source)
        self.assertNotIn("D_BASE", rewritten)
        self.assertIn("extern float D_SHARED;", rewritten)
        subprocess.run(
            ["cc", "-std=c89", "-pedantic-errors", "-DVERSION_US", "-x", "c", "-fsyntax-only", "-"],
            input=rewritten.encode(),
            check=True,
        )
        self.source.write_text(original)
        self.source.write_text(
            self.source.read_text()
            .replace("extern float D_PRIVATE;", "extern float D_PRIVATE[1];")
            .replace("+ D_PRIVATE;", "+ D_PRIVATE[0];")
        )
        preview = io.StringIO()
        with contextlib.redirect_stdout(preview):
            self.assertFalse(
                rodata.run(
                    argparse.Namespace(verb="migrate", function="alpha", apply=False, stage=False),
                    self.project,
                    cast(Policy, None),
                )
            )
        self.assertIn("return D_SHARED + 2.0f;", preview.getvalue())
        self.source.write_text(
            self.source.read_text() + "const float *private_address(void) { return &D_PRIVATE[0]; }\n"
        )
        args = argparse.Namespace(verb="migrate", function="alpha", apply=True, stage=True)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertFalse(rodata.run(args, self.project, cast(Policy, None)))
        source = self.source.read_text()
        self.assertIn("extern float D_SHARED;", source)
        self.assertNotIn("D_PRIVATE", source)
        self.assertIn("defined(VERSION_US)", source)
        self.assertIn("2.0f", source)
        self.assertIn("3.0f", source)
        subprocess.run(
            ["cc", "-std=c89", "-pedantic-errors", "-DVERSION_US", "-fsyntax-only", str(self.source)], check=True
        )
        for version in self.fixture.versions:
            _, _, segments = split.layout(self.fixture.version(version).split)
            rows = [r for segment in segments for r in segment.rows]
            local = next(r for r in rows if r.kind == ".rodata")
            self.assertEqual(local.path, "alpha")
            self.assertEqual(local.start, 0x44)
            self.assertEqual(split.end(local), 0x48)
            self.assertEqual(
                split.address(local, self.fixture.version(version).split), self.mappings[version][0]["address"] + 4
            )
        with self.assertRaisesRegex(Held, "not in one resident bin"):
            migrate(self.project, "alpha")

        obj = Object(
            assemble(
                self.root,
                "local_pool",
                ".set noreorder\n.text\n"
                "lui $at,%hi(string)\naddiu $a0,$at,%lo(string)\n"
                "lui $at,%hi(table)\naddiu $v0,$at,%lo(table)\n"
                'case: jr $ra\nnop\n.section .rdata\nstring: .asciz "hi"\n'
                ".align 2\ntable: .word case,case\n",
            )
        )
        raw = b"hi\0\0" + struct.pack(">2I", 0x2010, 0x2010)
        normalized = b"hi\0\0" + struct.pack(">2I", 0x80002010, 0x80002010)
        target = {0: 0x3C018000, 4: 0x24243000, 8: 0x3C018000, 12: 0x24223004}

        def read_memory(address: int, size: int) -> bytes:
            return raw[address - 0x80003000 : address - 0x80003000 + size]

        def read_table(address: int, size: int) -> bytes:
            return normalized[address - 0x80003000 : address - 0x80003000 + size]

        original_object = obj.path.read_bytes()
        for _ in range(2):
            self.assertEqual(
                arrange(obj, ".rdata", target, 0x80002000, read_memory, read_table, emit_resident=True), 0x80003000
            )
            self.assertEqual(relocated(Object(obj.path), ".rdata", 0x80002000), raw)
        obj.path.write_bytes(original_object)
        image = bytearray(128)
        for offset, word in target.items():
            struct.pack_into(">I", image, offset, word)
        image[0x40 : 0x40 + len(raw)] = raw
        interval = {"address": 0x80002000, "start": 0, "end": 24, "rodata_address": 0x80003000}
        mappings = [{"address": 0x80003000, "start": 0x40, "end": 0x4C, "table_entry_bias": 0x80000000}]
        for _ in range(2):
            self.assertEqual(resident(Object(obj.path), interval, bytes(image), ".rdata", mappings), 0x80003000)
            self.assertEqual(relocated(Object(obj.path), ".rdata", 0x80002000), raw)
