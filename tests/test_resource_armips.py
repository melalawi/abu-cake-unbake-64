"""Actual symbolic audio/graphics pairs retain execution overlays and distinct ROM extents."""

import gzip
import hashlib
import json
from pathlib import Path
from unittest.mock import patch

from tests.project_fixture import ProjectCase
from unbake import buildfiles
from unbake.config import Held
from unbake.decomp import checks
from unbake.project import publication_push

FIXTURE = Path(__file__).parent / "fixtures/resource_armips"
MANIFEST = json.loads((FIXTURE / "manifest.json").read_text())
GFX_FLAGS = (
    "-P -Iresources/rsp/graphics/PR -Iresources/rsp/graphics/rsp -Iresources/rsp/graphics "
    "-D_LANGUAGE_ASSEMBLY -DF3DEX_GBI_2 -DCFG_NoN=1 -DCFG_OLD_TRI_WRITE=1 "
    "-DBUG_CLIPPING_FAIL_WHEN_SUM_ZERO=1 -DBUG_FAIL_IF_CARRY_SET_AT_INIT=1"
)
GFX_STRING = "RSP Gfx ucode F3DEX.NoN   fifo 2.05  Yoshitaka Yasumoto 1998 Nintendo."


class ArmipsResourceTests(ProjectCase):
    versions = ("us",)

    def setUp(self):
        super().setUp()
        for item in MANIFEST["inputs"]:
            raw = gzip.decompress((FIXTURE / item["fixture"]).read_bytes())
            self.assertEqual(hashlib.sha256(raw).hexdigest(), item["sha256"])
            path = self.project.root / item["path"]
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(raw)
        self.text = "segments:\n"
        rom = bytearray(0xE1720)
        for row in sorted(MANIFEST["extents"], key=lambda r: r["rom_start"]):
            output = "code" if row["name"].endswith("_text") else "data"
            self.text += (
                f"  - name: {row['name']}\n    type: resource\n    start: 0x{row['rom_start']:X}\n"
                f"    end: 0x{row['rom_end']:X}\n    vram: 0x{row['resident_lma']:X}\n"
                f"    execution_vram: 0x{row['execution_vma']:X}\n"
                f"    resource_assembler: armips\n    resource_output: {output}\n"
            )
            if row["name"].startswith("graphics"):
                self.text += f'    resource_cppflags: "{GFX_FLAGS}"\n'
                self.text += f"    resource_asflags: \"-strequ ID_STR '{GFX_STRING}'\"\n"
            self.text += f'    subsegments:\n      - [0x{row["rom_start"]:X}, resource, "{row["source"]}"]\n'
            raw = gzip.decompress((FIXTURE / row["fixture"]).read_bytes())
            self.assertEqual(hashlib.sha256(raw).hexdigest(), row["sha256"])
            self.assertEqual(len(raw), row["bytes"])
            rom[row["rom_start"] : row["rom_end"]] = raw
            name = "_".join(Path(row["source"]).relative_to("resources").with_suffix("").parts) + "_" + output
            native = self.project.build_link("us") / "resources" / (name + ".bin")
            native.parent.mkdir(parents=True, exist_ok=True)
            native.write_bytes(raw)
        self.text += "  - [0xE1720]\n"
        self.project.version("us").split.write_text(self.text)
        self.project.version("us").baserom.write_bytes(rom)
        self.changed = {"versions/us/game.yaml"}

    def git(self, project, *args):
        if args[:2] == ("diff", "--name-only"):
            return "\0".join(self.changed)
        if args[0] == "show":
            return "segments:\n  - [0xE1720]\n"
        return ""

    def admit(self):
        with patch.object(publication_push, "_git", side_effect=self.git):
            return publication_push.admission(self.project, self.host, "head", "base")

    def test_actual_armips_pairs_group_outputs_and_preserve_symbolic_overlay_producer(self):
        resources = buildfiles.resource_bindings(self.project, "us")
        self.assertEqual(len(resources), 4)
        self.assertEqual(len(buildfiles.resource_pairs(tuple(resources))), 2)
        text = buildfiles.slices_mk(self.project, "us")
        self.assertEqual(text.count(" &: "), 2)
        self.assertEqual(text.count("$(ARMIPS)"), 2)
        self.assertIn("$(CPP) " + GFX_FLAGS, text)
        self.assertIn("-strequ ID_STR '" + GFX_STRING + "'", text)
        self.assertIn("-strequ CODE_FILE build/us/resources/rsp_audio_code.bin.tmp", text)
        self.assertIn("-strequ DATA_FILE build/us/resources/rsp_audio_data.bin.tmp", text)
        self.assertIn("resources/rsp/rsp_defs.inc", text)
        self.assertIn("resources/rsp/graphics/PR/gbi.h", text)
        self.assertNotIn("$(RESOURCE_BIN)", text)
        source = self.project.root / "resources/rsp/graphics/f3dex2.s"
        self.assertIn(".headersize 0x00001000 - orga()", source.read_text())
        self.assertEqual(checks.resource_opcodes(source), [])
        self.assertEqual(buildfiles.data_bindings(self.project, "us"), {})
        with patch.object(buildfiles, "n64link_pin", return_value=buildfiles.N64LINK_RELEASE):
            generated = buildfiles.generate(self.project, self.host)
        self.assertFalse(any(p.parent.name == "resources" and p.suffix == ".ld" for p in generated))

    def test_actual_10384_native_bytes_are_selected_by_registration_source_or_include_changes(self):
        result = self.admit()
        self.assertEqual(result["work"]["native_bytes_read"], 10384)
        self.assertEqual(result["work"]["rom_bytes_read"], 10384)
        self.assertEqual(result["work"]["resource_extents_compared"], 4)
        self.changed = {"resources/rsp/audio.s"}
        self.assertEqual([r["resource"] for r in self.admit()["scopes"]], ["rsp_audio_code", "rsp_audio_data"])
        self.changed = {"resources/rsp/rsp_defs.inc"}
        self.assertEqual(len(self.admit()["scopes"]), 4)

    def test_actual_pair_refuses_missing_output_native_mismatch_and_opcode_laundering(self):
        meta = self.project.version("us")
        meta.split.write_text(self.text.replace("resource_output: data", "resource_output: unselected", 1))
        with self.assertRaises(Held):
            buildfiles.resource_bindings(self.project, "us")
        meta.split.write_text(self.text)
        native = self.project.build_link("us") / "resources/rsp_audio_data.bin"
        raw = native.read_bytes()
        native.write_bytes(raw[:-1])
        with self.assertRaises(Held) as held:
            self.admit()
        self.assertEqual(held.exception.key, "publish.native_mismatch")
        native.write_bytes(raw)
        source = self.project.root / "resources/rsp/audio.s"
        source.write_text(source.read_text() + "\n.create CODE_FILE, 0x1080\n.dw 0x09000419\n.close\n")
        with self.assertRaises(Held) as held:
            self.admit()
        self.assertEqual(held.exception.key, "publish.push_rules")

    def test_assembler_arguments_quote_literal_strings_and_refuse_make_expansion(self):
        self.assertEqual(
            buildfiles.resource_args(("-strequ", "ID_STR", GFX_STRING)), "-strequ ID_STR '" + GFX_STRING + "'"
        )
        for unsafe in ("$(shell touch x)", "value#comment", "one\ntwo"):
            with self.assertRaises(Held):
                buildfiles.resource_args((unsafe,))
