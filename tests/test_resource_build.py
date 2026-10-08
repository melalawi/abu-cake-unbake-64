"""Actual symbolic RSP boot with separate execution VMA and host-resident LMA."""

import hashlib
import json
import re
from pathlib import Path
from unittest.mock import patch

from tests.project_fixture import ProjectCase
from unbake import buildfiles
from unbake.config import Held
from unbake.decomp import checks
from unbake.layout import split
from unbake.project import publication_push

FIXTURE = Path(__file__).parent / "fixtures/resource_boot"
NATIVE = json.loads((FIXTURE / "native.json").read_text())


class ResourceBuildTests(ProjectCase):
    versions = ("us",)

    def setUp(self):
        super().setUp()
        self.source = self.project.root / "resources/rsp/boot.s"
        self.source.parent.mkdir(parents=True)
        self.source.write_bytes((FIXTURE / "boot.s").read_bytes())
        self.assertEqual(hashlib.sha256(self.source.read_bytes()).hexdigest(), NATIVE["source_sha256"])
        self.before = (
            "segments:\n  - name: resident\n    type: data\n    start: 0xDD840\n"
            "    vram: 0x800DCC40\n    subsegments:\n      - [0xDD840, data, opaque]\n  - [0xDD910]\n"
        )
        self.registered = (
            self.before.replace("type: data", "type: resource")
            .replace(
                "    subsegments:",
                "    execution_vram: 0x04001000\n    resource_section: .rsp.boot\n"
                '    resource_asflags: "-EB -mips1"\n    subsegments:',
            )
            .replace("data, opaque", 'resource, "resources/rsp/boot.s"')
        )
        self.project.version("us").split.write_text(self.registered)
        body = (FIXTURE / "boot.bin").read_bytes()
        self.assertEqual(hashlib.sha256(body).hexdigest(), NATIVE["sha256"])
        self.assertEqual(len(body), 208)
        self.native = self.project.build_link("us") / "resources/rsp_boot.bin"
        self.native.parent.mkdir(parents=True)
        self.native.write_bytes(body)
        self.project.version("us").baserom.write_bytes(bytes(NATIVE["rom_start"]) + body)
        self.changed = {"versions/us/game.yaml"}

    def git(self, project, *args):
        if args[:2] == ("diff", "--name-only"):
            return "\0".join(self.changed)
        if args[0] == "show":
            return self.before
        return ""

    def admit(self):
        with patch.object(publication_push, "_git", side_effect=self.git):
            return publication_push.admission(self.project, self.host, "head", "base")

    def test_actual_boot_piece_and_generated_script_preserve_execution_and_resident_addresses(self):
        resource = next(iter(buildfiles.resource_bindings(self.project, "us")))
        self.assertEqual((resource.start, resource.size), (NATIVE["rom_start"], 208))
        self.assertEqual(resource.address, NATIVE["resident_lma"])
        self.assertEqual(resource.execution_address, NATIVE["execution_vma"])
        self.assertEqual(resource.asflags, ("-EB", "-mips1"))
        text = buildfiles.slices_mk(self.project, "us")
        self.assertIn("build/us/resources/rsp_boot.bin", text)
        self.assertIn("us.R.rsp_boot := 0x800DCC40:0xDD840:0xD0", text)
        raw = [(int(a), int(b)) for a, b in re.findall(r"^us\.S\.\w+ := (\d+) (\d+)$", text, re.M)]
        self.assertEqual(raw, [(0, NATIVE["rom_start"])])
        self.assertEqual(split.functions(self.project, "us"), [])
        self.assertEqual(buildfiles.data_bindings(self.project, "us"), {})
        script = buildfiles.resource_link_script(self.project, "us", resource)
        self.assertIn(".resource 0x4001000 : AT(0x800DCC40)", script)
        self.assertIn("KEEP(*(.rsp.boot))", script)
        self.assertIn("ASSERT(SIZEOF(.resource) == 208", script)
        self.assertNotIn("N64LINK", text)

    def test_actual_resource_metadata_only_admission_and_rebinding_compare_208_final_native_bytes(self):
        result = self.admit()
        self.assertEqual(result["work"]["native_bytes_read"], 208)
        self.assertEqual(result["work"]["rom_bytes_read"], 208)
        self.assertEqual(result["work"]["resource_extents_compared"], 1)
        self.assertEqual(result["scopes"][0]["execution_vma"], NATIVE["execution_vma"])
        self.assertEqual(result["scopes"][0]["resident_lma"], NATIVE["resident_lma"])
        self.before = self.registered
        self.assertEqual(self.admit()["scopes"], [])
        self.changed = {"resources/rsp/boot.s"}
        self.assertEqual(len(self.admit()["scopes"]), 1)
        self.changed = {"versions/us/game.yaml"}
        self.before = self.registered.replace("0x04001000", "0x04001004")
        self.assertEqual(len(self.admit()["scopes"]), 1)

    def test_actual_resource_refuses_missing_short_mismatched_native_and_source_hygiene(self):
        body = self.native.read_bytes()
        for changed in (body[:-1], body + b"\0", bytes([body[0] ^ 1]) + body[1:]):
            self.native.write_bytes(changed)
            with self.assertRaises(Held) as held:
                self.admit()
            self.assertEqual(held.exception.key, "publish.native_mismatch")
        self.native.unlink()
        with self.assertRaises(Held) as held:
            self.admit()
        self.assertEqual(held.exception.key, "publish.native_missing")
        self.native.write_bytes(body)
        text = self.source.read_text()
        for bad in (
            '\n.incbin "boot.bin"\n',
            "\n.word 0x09000419\n",
            '\nvoid bad(){ __asm__("nop"); }\n',
            "\nvolatile int bad;\n",
        ):
            self.source.write_text(text + bad)
            with self.assertRaises(Held) as held:
                self.admit()
            self.assertEqual(held.exception.key, "publish.push_rules")
        self.source.write_text(text + '\n.section .rodata,"a"\ncoefficients: .word 32767, -32767\n')
        self.assertEqual(checks.resource_opcodes(self.source), [])

    def test_actual_resource_binding_refuses_missing_source_unsafe_paths_and_ambiguous_outputs(self):
        meta = self.project.version("us")
        for bad in (
            self.registered.replace('"resources/rsp/boot.s"', '"resources/../../outside.s"'),
            self.registered.replace("execution_vram: 0x04001000", "execution_vram: -1"),
            self.registered.replace("resource_section: .rsp.boot", "resource_section: *"),
            self.registered.replace(
                '      - [0xDD840, resource, "resources/rsp/boot.s"]',
                '      - [0xDD840, resource, "resources/rsp/boot.s"]\n'
                '      - [0xDD900, resource, "resources/rsp/boot.s"]',
            ),
        ):
            meta.split.write_text(bad)
            with self.assertRaises(Held):
                buildfiles.resource_bindings(self.project, "us")
        meta.split.write_text(self.registered)
        self.source.unlink()
        with self.assertRaises(Held):
            buildfiles.units(self.project, "us")

    def test_resource_target_uses_symbolic_assembler_linker_section_and_size_only(self):
        text = buildfiles.makefile(self.project, self.host)
        recipe = text[text.index("RESOURCE_BIN =") : text.index("SLICE =")]
        self.assertIn("$(AS)", recipe)
        self.assertIn("$(LD) -EB -T versions/$(VER)/resources/$(RESOURCE_NAME).ld", recipe)
        self.assertIn("--only-section=.resource", recipe)
        self.assertIn("wc -c", recipe)
        self.assertNotIn("BASEROM", recipe)
        self.assertNotIn("N64LINK", recipe)
        self.assertNotIn("COMPILE", recipe)
        with patch.object(buildfiles, "n64link_pin", return_value=buildfiles.N64LINK_RELEASE):
            generated = buildfiles.generate(self.project, self.host)
        self.assertIn(self.project.root / "versions/us/resources/rsp_boot.ld", generated)
