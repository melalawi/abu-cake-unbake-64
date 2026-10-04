"""Read-only ownership and private string/table layout regressions."""

import argparse
import contextlib
import io
import json
import struct
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import patch

from tests.decomp.support import assemble
from tests.layout.test_split import ProjectFixture
from unbake.cli import rodata
from unbake.config import Policy, Project
from unbake.project_tools.elf import Object
from unbake.project_tools.layout import resident
from unbake.project_tools.literal_layout import arrange
from unbake.project_tools.rodata import relocated


class RodataCommandTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
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

    def test_private_string_and_biased_table_layout_is_repeatable(self) -> None:
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
