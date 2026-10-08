"""Captured 96-byte RDP source, its authored SDK providers and accepted final native output."""

import gzip
import hashlib
import json
from pathlib import Path
from unittest.mock import patch

from tests.project_fixture import ProjectCase
from tests.test_resource_armips import ArmipsResourceTests
from tests.test_standalone_data_progress import prepare
from unbake import buildfiles, cdecl, process
from unbake.project import publication_push
from unbake.project.headers import Graph
from unbake.report import data, progress

FIXTURE = Path(__file__).parent / "fixtures/standalone_provider_macros"
MANIFEST = json.loads((FIXTURE / "manifest.json").read_text())


class StandaloneProviderMacroTests(ProjectCase):
    versions = ("us",)

    def setUp(self):
        super().setUp()
        self.native = {}
        for row in MANIFEST["files"]:
            raw = gzip.decompress((FIXTURE / row["fixture"]).read_bytes())
            self.assertEqual(hashlib.sha256(raw).hexdigest(), row["sha256"])
            if row["path"].startswith("native."):
                self.native[row["path"].split(".")[1]] = raw
            else:
                path = self.project.root / row["path"]
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(raw)
        start, address = MANIFEST["rom_start"], MANIFEST["address"]
        self.project.version("us").split.write_text(
            f"segments:\n  - name: resident\n    type: data\n    start: 0x{start:X}\n    vram: 0x{address:X}\n"
            f'    subsegments:\n      - [0x{start:X}, data, "{MANIFEST["source"]}"]\n  - [0x{start + 96:X}]\n'
        )
        self.project.version("us").baserom.write_bytes(bytes(start) + self.native["bin"])
        prepare(self)
        (self.project.root / "versions/us/fixture.data.ld").write_text("SECTIONS { .data : { *(.rodata) } }\n")
        self.unit = next(iter(buildfiles.data_bindings(self.project, "us")))
        for ext, raw in self.native.items():
            path = self.project.build_link("us") / "data" / (self.unit.name + "." + ext)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(raw)

    def test_actual_authored_provider_macros_retain_definition_and_export_96_native_bytes_without_replay(self):
        source = self.project.root / MANIFEST["source"]
        sizes = {}
        with patch.object(process, "run_native") as native:
            definitions = Graph.capture(self.project).initialized_definitions(self.project, source, "us", sizes=sizes)
            self.assertIn("D_800CBCF0_de", definitions)
            proof = data.capture_producer(self.project, "us", self.unit, self.native["bin"], self.native["bin"])
            self.assertIsNotNone(proof)
            data.record_producers(self.project, [proof])
            report = progress.measure(self.project, None, "us")
            self.assertEqual(report["measures"]["matched_data"], 96)
            self.assertEqual(report["measures"]["complete_data"], 96)
            self.assertEqual(report["units"][0]["metadata"]["source_path"], MANIFEST["source"])
        native.assert_not_called()

    def test_unavailable_optional_definition_evidence_does_not_block_accepted_native_publication(self):
        def git(project, *args):
            if args[:2] == ("diff", "--name-only"):
                return "versions/us/game.yaml"
            if args[0] == "show":
                return f"segments:\n  - [0x{MANIFEST['rom_start'] + 96:X}]\n"
            return ""

        actual = cdecl.parser

        def parser(*args, **kwargs):
            parsed = actual(*args, **kwargs)
            parsed.parse = lambda *a, **kw: (_ for _ in ()).throw(cdecl.ParseError("unsupported provider"))
            return parsed

        with patch.object(publication_push, "_git", side_effect=git), patch.object(cdecl, "parser", side_effect=parser):
            result = publication_push.admission(self.project, self.host, "head", "base")
        self.assertTrue(result["ok"])
        self.assertEqual(result["work"]["data_extents_compared"], 1)
        self.assertEqual(result["native_data"], [])


class StandaloneArmipsEvidenceTests(ArmipsResourceTests):
    def test_actual_armips_pairs_preserve_exec_overlay_and_rom_storage_in_data_only_credit(self):
        prepare(self)
        result = self.admit()
        self.assertEqual(len(result["native_data"]), 4)
        bindings = [proof["payload"]["evidence"][0]["producer"]["unit"] for proof in result["native_data"]]
        self.assertTrue(any(binding["execution_address"] == 0x04001080 for binding in bindings))
        for binding in bindings:
            self.assertNotEqual(binding["execution_address"], binding["address"])
        data.record_producers(self.project, result["native_data"])
        report = progress.measure(self.project, None, "us")
        self.assertEqual((report["measures"]["matched_data"], report["measures"]["complete_data"]), (10384, 10384))
        self.assertEqual((report["measures"]["matched_code"], report["measures"]["total_functions"]), (0, 0))
        self.assertTrue(
            all(unit["metadata"]["progress_categories"] == ["data", "resource"] for unit in report["units"])
        )
