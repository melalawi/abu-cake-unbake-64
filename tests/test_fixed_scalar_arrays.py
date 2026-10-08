"""Replay the retained native 32-byte fixed-array sizing failure without native work."""

import gzip
import hashlib
import json
import os
from pathlib import Path
from unittest.mock import patch

import toml

from tests.project_fixture import ProjectCase
from unbake import buildfiles, config, process
from unbake.config import Held
from unbake.objects.elf import Object
from unbake.project import publication_push
from unbake.project.headers import Graph
from unbake.report import data

FIXTURE = Path(__file__).parent / "fixtures/fixed_scalar_arrays"
RECORDS = json.loads((FIXTURE / "manifest.json").read_text())


class FixedScalarArrayTests(ProjectCase):
    versions = ("us-rev1",)

    def test_retained_four_float_arrays_add_exact_32_bytes_to_native_producer_extents(self):
        version = self.versions[0]
        meta = self.project.version(version)
        rows = sorted(RECORDS, key=lambda row: row["rom_start"])
        self.before = (
            "name: fixture\nsegments:\n"
            "  - [0x0, header, header]\n"
            "  - name: main\n    type: code\n"
            f"    start: 0x{rows[0]['rom_start']:X}\n    vram: 0x{rows[0]['address']:X}\n"
            "    subsegments:\n"
            f"      - [0x{rows[0]['rom_start']:X}, rodata, raw]\n"
            f"  - [0x{rows[-1]['rom_start'] + rows[-1]['bytes']:X}]\n"
        )
        owned = ""
        rom = bytearray(rows[-1]["rom_start"] + rows[-1]["bytes"])
        for row in rows:
            payloads = {}
            for ext, info in row["files"].items():
                raw = (FIXTURE / info["fixture"]).read_bytes()
                raw = gzip.decompress(raw) if ext == "elf" else raw
                self.assertEqual(hashlib.sha256(raw).hexdigest(), info["sha256"])
                payloads[ext] = raw
            (self.project.src / (row["unit"] + ".c")).write_bytes(payloads["c"])
            native = self.project.build_link(version) / "data" / row["unit"]
            native.parent.mkdir(parents=True, exist_ok=True)
            native.with_suffix(".elf").write_bytes(payloads["elf"])
            native.with_suffix(".bin").write_bytes(payloads["bin"])
            obj = Object(native.with_suffix(".elf"))
            index = obj.section(".data")
            labels = [s for table in obj.symbols.values() for s in table if s["section"] == index and s["name"]]
            self.assertTrue(labels)
            self.assertTrue(all(s["info"] & 15 == 0 and s["size"] == 0 for s in labels))
            self.assertEqual(obj.content(index), payloads["bin"])
            rom[row["rom_start"] : row["rom_start"] + row["bytes"]] = payloads["bin"]
            owned += f'      - [0x{row["rom_start"]:X}, rodata, "src/{row["unit"]}.c"]\n'
            if row != rows[-1]:
                owned += f"      - [0x{row['rom_start'] + row['bytes']:X}, rodata, raw_gap]\n"
        meta.split.write_text(self.before.replace(f"      - [0x{rows[0]['rom_start']:X}, rodata, raw]\n", owned))
        meta.baserom.write_bytes(rom)
        values = toml.load(self.project.root / "config.toml")
        values["version"][version]["baserom_sha1"] = hashlib.sha1(rom).hexdigest()
        (self.project.root / "config.toml").write_text(toml.dumps(values))
        self.project = config.load(self.project.root)
        for relative in data.required(self.project, version):
            path = self.project.root / relative
            if not path.exists():
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("native fixture recipe\n")
        (self.project.root / f"versions/{version}/fixture.data.ld").write_text(
            buildfiles.data_link_script(self.project, version)
        )
        # Make freshness follows the retained source inputs; no compiler is run.
        for row in rows:
            path = self.project.build_link(version) / "data" / (row["unit"] + ".bin")
            os.utime(path, None)

        def git(project, *args):
            if args[:2] == ("diff", "--name-only"):
                return meta.split.relative_to(project.root).as_posix()
            if args[0] == "show":
                return self.before
            return ""

        with patch.object(publication_push, "_git", side_effect=git), patch.object(process, "run_native") as native:
            accepted = publication_push.admission(self.project, self.host, "head", "base")
            proofs = accepted["native_data"]
            self.assertEqual(len(proofs), 2)
            extents = [e for proof in proofs for e in proof["payload"]["evidence"][0]["extents"]]
            arrays = [e for e in extents if e["size"] == 8]
            self.assertEqual({e["symbol"] for e in arrays}, {"D_800DE448", "D_800DE450_de", "D_800DE458", "D_800E2280"})
            self.assertEqual(sum(e["size"] for e in arrays), 32)
            self.assertEqual(sum(e["size"] for e in extents), 44)
            self.assertEqual(accepted["work"]["native_bytes_read"], 44)
            self.assertEqual(accepted["work"]["rom_bytes_read"], 44)
            data.record_producers(self.project, proofs)
            sources = {row["unit"]: (row["files"]["c"]["sha256"], set(), False) for row in rows}
            coverage = data.coverage(self.project, version, sources, data.snapshots(self.project)[version])
            self.assertEqual(coverage.manifest["verified_bytes"], 44)
            changed = self.project.src / (rows[0]["unit"] + ".c")
            changed.write_bytes(changed.read_bytes() + b"\n")
            sources[rows[0]["unit"]] = (hashlib.sha256(changed.read_bytes()).hexdigest(), set(), False)
            coverage = data.coverage(self.project, version, sources, data.snapshots(self.project)[version])
            self.assertEqual(coverage.manifest["verified_bytes"], 24)
            # Ordinary source hygiene still refuses these exact producers.
            changed.write_bytes(changed.read_bytes() + b"volatile int forbidden = 1;\n")
            with self.assertRaises(Held):
                publication_push.admission(self.project, self.host, "head", "base")
        native.assert_not_called()

    def test_only_positive_literal_scalar_bounds_receive_sizes(self):
        source = self.project.src / "bounds.c"
        source.write_text(
            "typedef float scalar;\n"
            "const scalar fixed[2] = {1.0f, 2.0f};\n"
            "const double hex[0x2U] = {1.0, 2.0};\n"
            "const short octal[02L] = {1, 2};\n"
            "const float incomplete[] = {1.0f, 2.0f};\n"
            "const float unknown[COUNT] = {1.0f, 2.0f};\n"
            "const float variable[n] = {1.0f, 2.0f};\n"
            "const float expression[1 + 1] = {1.0f, 2.0f};\n"
            "const float zero[0] = {};\n"
            "const float negative[-2] = {1.0f, 2.0f};\n"
            "const float matrix[2][2] = {{1.0f, 2.0f}, {3.0f, 4.0f}};\n"
            "float *pointers[2] = {0, 0};\n"
        )
        sizes = {}
        definitions = Graph.capture(self.project).initialized_definitions(self.project, source, "us-rev1", sizes=sizes)
        self.assertEqual(len(definitions), 11)
        self.assertEqual(sizes, {"fixed": 8, "hex": 16, "octal": 4})
