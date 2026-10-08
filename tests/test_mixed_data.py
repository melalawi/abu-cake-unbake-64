"""Focused mixed producer units on retained immutable source and real native ELF/results."""

import gzip
import hashlib
import io
import json
import os
import struct
import subprocess
import sys
import zipfile
from pathlib import Path
from unittest.mock import patch

import toml

from tests.elf_fixture import linked_fixture, write_object
from tests.project_fixture import ProjectCase
from unbake import buildfiles, config, process
from unbake.config import Held
from unbake.objects import mixed
from unbake.objects.elf import Object
from unbake.project import publication_push
from unbake.report import data, verify

FIXTURE = Path(__file__).parent / "fixtures/mixed_data"
CASE = json.loads((FIXTURE / "manifest.json").read_text())


class MixedDataTests(ProjectCase):
    versions = ("us-rev1",)

    def setUp(self):
        super().setUp()
        with zipfile.ZipFile(FIXTURE / "closure.zip") as archive:
            for name in archive.namelist():
                content = archive.read(name)
                self.assertEqual(hashlib.sha256(content).hexdigest(), CASE["files"][name]["sha256"])
                path = self.project.root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(content)
        self.name = CASE["function"]
        self.source = self.project.src / (self.name + ".c")
        self.assertEqual(hashlib.sha256(self.source.read_bytes()).hexdigest(), CASE["source_sha256"])
        self.original = self.project.root / "build/cas" / (CASE["key"] + ".o")
        self.original.parent.mkdir(parents=True)
        self.original.write_bytes(gzip.decompress((FIXTURE / "original.elf.gz").read_bytes()))
        self.assertEqual(hashlib.sha256(self.original.read_bytes()).hexdigest(), CASE["object_sha256"])
        directory = self.project.build_link("us-rev1") / "units"
        directory.mkdir(parents=True)
        self.placed = directory / (self.name + ".placed.o")
        self.placed.write_bytes(gzip.decompress((FIXTURE / "placed.elf.gz").read_bytes()))
        self.code = directory / (self.name + ".bin")
        self.code.write_bytes((FIXTURE / "text.bin").read_bytes())
        self.data_bin = self.project.build_link("us-rev1") / "data" / (self.name + ".bin")
        self.data_bin.parent.mkdir()
        self.derived = self.data_bin.with_suffix(".o")
        self.final = self.data_bin.with_suffix(".elf")
        key_path = self.project.build_link("us-rev1") / "src" / (self.name + ".key")
        key_path.parent.mkdir()
        key_path.write_text(CASE["key"] + "\n")
        self.options = dict(
            section=".rdata", offset=CASE["offset"], size=CASE["size"], bias=CASE["bias"], function=self.name
        )
        meta = self.project.version("us-rev1")
        self.before = (
            "name: fixture\nsegments:\n  - [0x0, header, header]\n"
            "  - name: code\n    type: code\n    start: 0x1EE7C\n    vram: 0x8021E27C\n"
            f"    subsegments:\n      - [0x1EE7C, c, {self.name}]\n"
            "  - [0x1F1D4]\n"
            "  - name: table\n    type: code\n    start: 0xC80E8\n    vram: 0x800C74E8\n"
            "    subsegments:\n      - [0xC80E8, rodata, raw_table]\n"
            "  - name: tail\n    type: code\n    start: 0xC827C\n    vram: 0x800C767C\n"
            "    subsegments:\n      - [0xC827C, rodata, separate_tail]\n  - [0xC828C]\n"
        )
        self.bound = self.before.replace(
            "    start: 0xC80E8\n",
            "    start: 0xC80E8\n    data_section: .rdata\n    data_offset: 24\n    data_pointer_bias: 0x80000000\n",
        ).replace("rodata, raw_table", f'rodata, "src/{self.name}.c"')
        meta.split.write_text(self.bound)
        meta.symbols.write_text(f"{self.name} = 0x8021E27C;\n")
        rom = bytearray(0xC828C)
        rom[CASE["text_start"] : CASE["text_start"] + 856] = self.code.read_bytes()
        rom[CASE["data_start"] : CASE["data_start"] + 404] = (FIXTURE / "table.bin").read_bytes()
        # The owned tail is outside this binding and remains a separate ordinary piece.
        rom[0xC827C:0xC828C] = bytes.fromhex("4100000042000000418000003f800000")
        meta.baserom.write_bytes(rom)
        values = toml.load(self.project.root / "config.toml")
        values["version"]["us-rev1"]["baserom_sha1"] = hashlib.sha1(rom).hexdigest()
        (self.project.root / "config.toml").write_text(toml.dumps(values))
        self.project = config.load(self.project.root)
        for relative in data.required(self.project, "us-rev1"):
            path = self.project.root / relative
            if not path.exists():
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("native fixture recipe\n")
        symbols = self.project.root / "versions/us-rev1/symbols.ld"
        symbols.write_bytes((FIXTURE / "symbols.ld").read_bytes())
        self.unit = buildfiles.data_units(self.project, "us-rev1")[0]
        (self.project.root / "versions/us-rev1/fixture.data.ld").write_text(
            buildfiles.data_link_script(self.project, "us-rev1")
        )
        script = self.project.root / "versions/us-rev1/data" / (self.name + ".ld")
        script.parent.mkdir()
        script.write_text(buildfiles.mixed_data_link_script(self.project, "us-rev1", self.unit))
        (self.project.tools / "report-verifier.zip").write_bytes(b"fixture content-pinned native helper\n")
        self.changed = {meta.split.relative_to(self.project.root).as_posix()}

    def native_link_outcome(self):
        """Mock GNU ld only: resolve the real retained relocations with actual symbols."""
        mixed.prepare(Object(self.original), Object(self.placed), self.derived, **self.options)
        obj = Object(self.derived)
        symbols_path = self.project.root / "versions/us-rev1/symbols.ld"
        symbols = data.linker_symbols(symbols_path, symbols_path.read_text())
        for table_index, entries in obj.symbols.items():
            for symbol in entries:
                if symbol["section"] == 0 and symbol["name"]:
                    location = obj.sections[table_index][4] + 16 * symbol["index"]
                    struct.pack_into(">I", obj.data, location + 4, symbols[symbol["name"]])
                    struct.pack_into(">H", obj.data, location + 14, 0xFFF1)
        resolved = self.root / "external-symbols.o"
        resolved.write_bytes(obj.data)
        linked_fixture(
            self.final, (resolved,), {".text": CASE["text_address"], ".rdata": CASE["data_address"] - CASE["offset"]}
        )
        final = Object(self.final)
        self.assertEqual(final.content(final.section(".text")), self.code.read_bytes())
        # The script's native output section is .data; all input section bytes are retained.
        write_object(
            self.final,
            {".text": final.content(final.section(".text")), ".data": final.content(final.section(".rdata"))},
            addresses={".text": CASE["text_address"], ".data": CASE["data_address"] - CASE["offset"]},
            linked=True,
        )
        content = mixed.extract(
            Object(self.original), Object(self.final), self.code.read_bytes(), text=CASE["text_address"], **self.options
        )
        self.data_bin.write_bytes(content)
        self.assertEqual(content, (FIXTURE / "table.bin").read_bytes())

    def git(self, project, *args):
        if args[:2] == ("diff", "--name-only"):
            return "\0".join(self.changed)
        if args[0] == "show":
            return self.before
        return ""

    def admit(self):
        with patch.object(publication_push, "_git", side_effect=self.git):
            return publication_push.admission(self.project, self.host, "head", "base")

    def test_real_101_relocations_keep_code_vma_extract_404_and_preserve_original_objects(self):
        originals = (self.original.read_bytes(), self.placed.read_bytes())
        self.native_link_outcome()
        original, derived = Object(self.original), Object(self.derived)
        self.assertEqual(len(original.relocations(original.section(".rdata"))), 101)
        self.assertEqual(
            derived.relocations(derived.section(".rdata")), original.relocations(original.section(".rdata"))
        )
        self.assertEqual(
            derived.content(derived.section(".text")), Object(self.placed).content(Object(self.placed).section(".text"))
        )
        self.assertEqual((self.original.read_bytes(), self.placed.read_bytes()), originals)
        self.assertEqual(self.data_bin.stat().st_size, 404)
        self.assertEqual(len(Object(self.final).content(Object(self.final).section(".data"))), 444)
        for options in (
            {**self.options, "size": 420},
            {**self.options, "offset": 28},
            {**self.options, "bias": 4},
            {**self.options, "function": "invented_label"},
        ):
            with self.assertRaises(ValueError):
                mixed.prepare(Object(self.original), Object(self.placed), self.derived, **options)
        with self.assertRaises(ValueError):
            mixed.prepare(Object(self.original), Object(self.placed), self.original, **self.options)

    def test_actual_mixed_make_pieces_bind_only_table_and_reuse_published_code_cas(self):
        self.assertIsInstance(self.unit, buildfiles.MixedData)
        self.assertEqual(
            (self.unit.offset, self.unit.size, self.unit.code_address, self.unit.code_size), (24, 404, 0x8021E27C, 856)
        )
        text = buildfiles.slices_mk(self.project, "us-rev1")
        self.assertEqual(text.count(f"  build/us-rev1/data/{self.name}.bin"), 1)
        self.assertEqual(text.count(f"  build/us-rev1/units/{self.name}.bin"), 1)
        self.assertIn("us-rev1.S.000C827C := 819836 16", text)
        self.assertIn(f"build/us-rev1/data/{self.name}.bin: build/us-rev1/units/{self.name}.bin", text)
        script = buildfiles.mixed_data_link_script(self.project, "us-rev1", self.unit)
        self.assertIn(".text 0x8021E27C", script)
        self.assertIn(".data 0x800C74D0", script)
        self.assertIn("*(.rdata)", script)
        recipe = buildfiles.makefile(self.project, self.host)
        self.assertIn("PYTHONPATH=tools/report-verifier.zip python3 -m unbake.objects.mixed", recipe)
        self.assertIn("$(MIXED_TOOL) prepare --original build/cas/$$key.o", recipe)
        self.assertIn("--placed build/$(VER)/units/$(*F).placed.o", recipe)
        self.assertIn("$(MIXED_TOOL) extract --original build/cas/$$key.o", recipe)
        self.assertNotIn("BASEROM", recipe[recipe.index("MIXED_TOOL =") : recipe.index("RESOURCE_NAME =")])
        # The Make helper runs directly from the canonical pinned zip on plain
        # Python, with no source checkout or installed third-party packages.
        generated = buildfiles.generate(self.project, self.host)
        payload = generated[self.project.tools / verify.BUNDLE.removeprefix("tools/")]
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            self.assertEqual(archive.read("unbake/objects/mixed.py"), Path(mixed.__file__).read_bytes())
        bundle = self.root / "native-helper.zip"
        bundle.write_bytes(payload)
        arguments = [
            "--original",
            str(self.original),
            "--section",
            ".rdata",
            "--offset",
            "24",
            "--size",
            "404",
            "--bias",
            "0x80000000",
            "--function",
            self.name,
            "--text",
            "0x8021E27C",
        ]
        self.native_link_outcome()
        for operation, paths in (
            ("prepare", ["--placed", str(self.placed), "--output", str(self.root / "bundled.o")]),
            (
                "extract",
                ["--final", str(self.final), "--code", str(self.code), "--output", str(self.root / "bundled.bin")],
            ),
        ):
            result = subprocess.run(
                [sys.executable, "-S", "-m", "unbake.objects.mixed", operation, *arguments, *paths],
                cwd=self.root,
                env={**os.environ, "PYTHONPATH": str(bundle)},
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual((self.root / "bundled.o").read_bytes(), self.derived.read_bytes())
        self.assertEqual((self.root / "bundled.bin").read_bytes(), self.data_bin.read_bytes())
        self.assertEqual(generated[self.project.root / f"versions/us-rev1/data/{self.name}.ld"].decode(), script)
        for replacement in ("data_offset: 25", "data_pointer_bias: 4", "data_section: .text"):
            field = replacement.split(":")[0]
            current = {"data_offset": "24", "data_pointer_bias": "0x80000000", "data_section": ".rdata"}[field]
            with self.assertRaises(Held):
                buildfiles.data_bindings(
                    self.project, "us-rev1", text=self.bound.replace(field + ": " + current, replacement)
                )
        with self.assertRaises(Held):
            buildfiles.data_bindings(self.project, "us-rev1", text=self.bound.replace(", c,", ", asm,"))

    def test_actual_mixed_admission_current_proof_404_tail_excluded_and_source_hygiene(self):
        self.native_link_outcome()
        with patch.object(process, "run_native") as native:
            accepted = self.admit()
            self.assertEqual(
                accepted["work"],
                {"native_bytes_read": 404, "rom_bytes_read": 404, "functions_compared": 0, "data_extents_compared": 1},
            )
            self.assertEqual(len(accepted["native_data"]), 1)
            proof = accepted["native_data"][0]
            extent = proof["payload"]["evidence"][0]["extents"][0]
            self.assertEqual((extent["rom_offset"], extent["size"]), (0xC80E8, 404))
            self.assertEqual(extent["symbol"], self.name)
            data.record_producers(self.project, [proof])
            sources = {self.name: (CASE["source_sha256"], set(), False)}
            coverage = data.coverage(self.project, "us-rev1", sources, data.snapshots(self.project)["us-rev1"])
            self.assertEqual(coverage.manifest["verified_bytes"], 404, coverage.manifest)
            self.assertEqual(coverage.matched, ((0xC80E8, 0xC827C),))
            self.changed = {"tools/report-verifier.zip"}
            self.assertEqual(len(self.admit()["scopes"]), 1)
            meta = self.project.version("us-rev1")
            meta.split.write_text(self.bound.replace("data_pointer_bias: 0x80000000", "data_pointer_bias: 0"))
            coverage = data.coverage(self.project, "us-rev1", sources, data.snapshots(self.project)["us-rev1"])
            self.assertEqual(coverage.manifest["verified_bytes"], 0)
            meta.split.write_text(self.bound)
            bundle = self.project.tools / "report-verifier.zip"
            saved_bundle = bundle.read_bytes()
            bundle.write_bytes(saved_bundle + b"changed helper\n")
            coverage = data.coverage(self.project, "us-rev1", sources, data.snapshots(self.project)["us-rev1"])
            self.assertEqual(coverage.manifest["verified_bytes"], 0)
            bundle.write_bytes(saved_bundle)
            self.assertIn("data.mixed", proof["dependencies"]["recipes"])
            # The immutable captured proof keeps actual native identities and
            # stays usable in a source-only checkout without ignored outputs.
            self.assertEqual(proof["dependencies"]["values"]["mixed_object_sha256"], CASE["object_sha256"])
            for path in (self.original, self.placed, self.code, self.data_bin, self.final):
                path.unlink()
            coverage = data.coverage(self.project, "us-rev1", sources, data.snapshots(self.project)["us-rev1"])
            self.assertEqual(coverage.manifest["verified_bytes"], 404, coverage.manifest)
            self.changed = {"src/" + self.name + ".c"}
            self.source.write_bytes(self.source.read_bytes() + b"\nvolatile int forbidden = 1;\n")
            with self.assertRaises(Held):
                self.admit()
        native.assert_not_called()

    def test_actual_mixed_missing_stale_mismatch_and_wrong_native_placements_refuse(self):
        self.native_link_outcome()
        original = self.data_bin.read_bytes()
        self.data_bin.write_bytes(original[:-1])
        with self.assertRaises(Held):
            self.admit()
        self.data_bin.write_bytes(original)
        self.data_bin.write_bytes(bytes([original[0] ^ 1]) + original[1:])
        with self.assertRaises(Held):
            self.admit()
        self.data_bin.write_bytes(original)
        obj = Object(self.final)
        text_index = obj.section(".text")
        struct.pack_into(">I", obj.data, obj.table + 40 * text_index + 12, CASE["text_address"] - CASE["bias"])
        self.final.write_bytes(obj.data)
        self.assertEqual(self.admit()["native_data"], [])
        self.native_link_outcome()
        obj = Object(self.final)
        data_index = obj.section(".data")
        struct.pack_into(">I", obj.data, obj.table + 40 * data_index + 12, CASE["data_address"])
        self.final.write_bytes(obj.data)
        self.assertEqual(self.admit()["native_data"], [])
        self.native_link_outcome()
        self.code.write_bytes(bytes([self.code.read_bytes()[0] ^ 1]) + self.code.read_bytes()[1:])
        self.data_bin.write_bytes(original)
        self.assertEqual(self.admit()["native_data"], [])
        self.code.write_bytes((FIXTURE / "text.bin").read_bytes())
        self.native_link_outcome()
        stamp = self.source.stat().st_mtime_ns
        os.utime(self.data_bin, ns=(stamp - 1, stamp - 1))
        self.assertEqual(self.admit()["native_data"], [])
        self.data_bin.unlink()
        with self.assertRaises(Held):
            self.admit()
