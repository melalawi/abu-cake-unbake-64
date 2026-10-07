"""Native final-data coverage on actual BT scalar and RW resident ELF/ROM slices."""

import copy
import hashlib
import struct
from pathlib import Path
from unittest.mock import patch

from tests.project_fixture import ProjectCase
from unbake import cdecl, config, inputs, process
from unbake.config import Held
from unbake.layout import split
from unbake.objects.elf import Object
from unbake.report import data, progress, state
from unbake.work import attempts

RW = Path(__file__).parent / "layout/fixtures/ragewars_resident_storage"
# Actual BT func_80099CD8 source initialized constants; big-endian stored ROM values.
BT_SOURCE = "const double D_800723B0 = 0.3;\nconst float D_800723B8 = 0.5f;\nconst float D_800723BC = 28672.0f;\n"
BT_BYTES = struct.pack(">dff", 0.3, 0.5, 28672.0)


class DataProgressTests(ProjectCase):
    versions = ("us", "us-rev1", "eu", "eu-x", "de")

    def setUp(self):
        super().setUp()
        self.start = 0x64
        self.end = self.start + len(BT_BYTES)
        self.address = 0x80001024
        (self.project.src / "constants.c").write_text(BT_SOURCE)
        for version in self.versions:
            meta = self.project.version(version)
            meta.baserom.write_bytes(meta.baserom.read_bytes() + BT_BYTES)
            meta.split.write_text(
                meta.split.read_text().replace("  - [0x64]", f"      - [0x64, rodata, constants]\n  - [0x{self.end:X}]")
            )
        import toml

        values = toml.load(self.project.root / "config.toml")
        for version in self.versions:
            values["version"][version]["baserom_sha1"] = hashlib.sha1(
                self.project.version(version).baserom.read_bytes()
            ).hexdigest()
        (self.project.root / "config.toml").write_text(toml.dumps(values))
        self.project = config.load(self.project.root)
        for version in self.versions:
            for name in (f"{self.project.name}.ld", "symbols.ld"):
                (self.project.root / "versions" / version / name).write_text("SECTIONS { .rodata : { *(.rodata) } }\n")
        for name in ("Makefile", "units.mk", "tools/n64link.version"):
            (self.project.root / name).write_text("fixture pinned native link\n")
        self.sources = {"constants": (hashlib.sha256(BT_SOURCE.encode()).hexdigest(), set(), False)}

    def payload(self, version="us", *, bytes_=BT_BYTES):
        meta = self.project.version(version)
        address = 0x80001024 if version == "us" else 0x80202024
        extent = {
            "version": version,
            "owner_source": {"root": "project", "parts": ["src", "constants.c"]},
            "symbol": "D_800723B0",
            "section": ".rodata",
            "address": address,
            "rom_offset": self.start,
            "size": len(bytes_),
            "source_sha256": self.sources["constants"][0],
            "bytes_sha256": hashlib.sha256(bytes_).hexdigest(),
            "rom_bytes_sha256": hashlib.sha256(BT_BYTES[: len(bytes_)]).hexdigest(),
            "definition_proof_id": data.digest({"source": self.sources["constants"][0], "version": version}),
        }
        return {
            "schema": 1,
            "version": version,
            "rom_sha1": meta.baserom_sha1,
            "rom_size": self.end,
            "evidence": [
                {
                    "final_artifact_sha256": data.digest({"final-linked-bytes": bytes_.hex()}),
                    "input_identity": data.digest({"version": version}),
                    "sections": [
                        {
                            "name": ".rodata",
                            "address": address,
                            "size": len(bytes_),
                            "sha256": hashlib.sha256(bytes_).hexdigest(),
                        }
                    ],
                    "extents": [extent],
                    "unavailable_reason": None,
                }
            ],
        }

    def record(self, payload, *, bind=True):
        version = payload["version"]
        paths = data.required(self.project, version) | {"src/constants.c", "include/types.h"}
        dependencies = inputs.DependencySet(
            tuple(
                inputs.file_pin(self.project.root / path, root=self.project.root, root_id="project", reuse=False)
                for path in sorted(paths)
            ),
            {
                "version": version,
                "compiler": self.project.compiler_reference("constants"),
                "dependencies_unknown": False,
            },
            {"data.extents": data.digest({"recipe": "final initialized extent versus ROM"})},
        )
        operation = attempts.Operation.make(self.project, "native.data", version, {}, dependencies)
        attempts.ledger(self.project).record(
            operation,
            attempts.Outcome(
                operation.id, "ok", {"native_data": payload}, None, {}, (data.digest(payload),) if bind else ()
            ),
        )
        return attempts.ledger(self.project).latest("native.data", version)

    def cover(self, payload=None, *, version="us", sources=None):
        record = self.record(self.payload(version) if payload is None else payload)
        return data.coverage(self.project, version, self.sources if sources is None else sources, record)

    def test_exact_initialized_bytes_all_five_holders_union_aliases_and_no_native_report_calls(self):
        for version in self.versions:
            self.record(self.payload(version))
        with (
            patch.object(process, "run_native") as native,
            patch.object(process, "run_tool") as tool,
            patch.object(split, "functions", wraps=split.functions) as scans,
            patch.object(cdecl, "declarations", wraps=cdecl.declarations) as parses,
            patch.object(data, "snapshots", wraps=data.snapshots) as snapshots,
        ):
            current = state.inventory(self.project)
            reports = {
                version: progress.measure(self.project, None, version, current=current) for version in self.versions
            }
        native.assert_not_called()
        tool.assert_not_called()
        self.assertEqual((scans.call_count, parses.call_count, snapshots.call_count), (5, 1, 1))
        for version, report in reports.items():
            self.assertEqual((report["measures"]["total_code"], report["measures"]["matched_code"]), (36, 0))
            self.assertEqual(
                (
                    report["measures"]["total_data"],
                    report["measures"]["matched_data"],
                    report["measures"]["complete_data"],
                ),
                (16, 16, 16),
            )
            self.assertEqual(report["measures"]["matched_data_percent"], 100.0)
            self.assertEqual(current.data_coverage[version].manifest["unmeasured_bytes"], 0)
            self.assertEqual(len([u for u in report["units"] if u["metadata"]["progress_categories"] == ["data"]]), 1)
        payload = self.payload()
        duplicate = copy.deepcopy(payload["evidence"][0]["extents"][0])
        duplicate["symbol"] = "alias"
        payload["evidence"][0]["extents"].append(duplicate)
        self.assertEqual(self.cover(payload).manifest["verified_bytes"], 16)

    def test_one_byte_final_mismatch_has_zero_credit_and_explicit_cause(self):
        payload = self.payload(bytes_=BT_BYTES[:-1] + bytes([BT_BYTES[-1] ^ 1]))
        coverage = self.cover(payload)
        self.assertEqual(coverage.manifest["verified_bytes"], 0)
        self.assertIn("data.bytes.mismatch", [cause["key"] for cause in coverage.manifest["causes"]])

    def test_missing_proof_and_initialized_source_presence_earn_nothing(self):
        coverage = data.coverage(self.project, "us", self.sources, None)
        self.assertEqual((coverage.manifest["verified_bytes"], coverage.manifest["unmeasured_bytes"]), (0, 16))
        self.assertEqual(coverage.manifest["causes"][0]["key"], "data.proof.missing")
        record = self.record(self.payload(), bind=False)
        self.assertEqual(data.coverage(self.project, "us", self.sources, record).manifest["verified_bytes"], 0)

    def test_guarded_candidate_source_and_original_assembly_emission_earn_nothing(self):
        guarded = {"constants": (self.sources["constants"][0], set(), True)}
        self.assertEqual(self.cover(sources=guarded).manifest["verified_bytes"], 0)
        payload = self.payload()
        payload["evidence"][0]["extents"] = []
        payload["evidence"][0]["unavailable_reason"] = "original assembly and raw ROM slices are emitted"
        self.assertEqual(self.cover(payload).manifest["verified_bytes"], 0)

    def test_changed_source_header_linker_flags_and_rom_invalidate_once(self):
        for relative in (
            "src/constants.c",
            "include/types.h",
            "config.toml",
            "layout.toml",
            "versions/us/fixture.ld",
            "tools/compilers.sha256",
            "tools/n64link.version",
            "roms/baserom.us.z64",
        ):
            with self.subTest(relative):
                record = self.record(self.payload())
                path = self.project.root / relative
                original = path.read_bytes()
                path.write_bytes(original + b" \n")
                coverage = data.coverage(self.project, "us", self.sources, record)
                self.assertEqual((coverage.manifest["verified_bytes"], coverage.manifest["invalidations"]), (0, 1))
                path.write_bytes(original)

    def test_bss_excluded_and_padding_has_no_implicit_credit(self):
        meta = self.project.version("us")
        original = meta.split.read_text()
        meta.split.write_text(
            original.replace(f"  - [0x{self.end:X}]", f"      - [0x{self.end:X}, bss, zero]\n  - [0x{self.end + 16:X}]")
        )
        intervals, _ = data.declared(self.project, "us")
        self.assertEqual(sum(row.end - row.start for row in intervals), 16)
        meta.split.write_text(original)
        payload = self.payload(bytes_=BT_BYTES[:8])
        coverage = self.cover(payload)
        self.assertEqual((coverage.manifest["verified_bytes"], coverage.manifest["unmeasured_bytes"]), (8, 8))

    def test_malformed_bounds_and_overlaps_fail_typed_before_credit(self):
        for edit in ("bounds", "overlap", "bss", "machine-path"):
            with self.subTest(edit):
                payload = self.payload()
                evidence = payload["evidence"][0]
                extent = evidence["extents"][0]
                if edit == "bounds":
                    extent["size"] = 100
                elif edit == "overlap":
                    other = copy.deepcopy(extent)
                    other.update(rom_offset=self.start + 4, address=extent["address"] + 4, size=4)
                    evidence["extents"].append(other)
                elif edit == "bss":
                    evidence["sections"][0]["name"] = ".bss"
                else:
                    extent["owner_source"]["parts"] = ["/root", "data.c"]
                with self.assertRaises(Held):
                    self.cover(payload)

    def test_relocated_pointer_uses_final_linked_bytes_not_unrelocated_object(self):
        from tests.elf_fixture import linked_fixture, write_object

        obj = write_object(
            self.root / "table.o",
            {".rodata": bytes(4)},
            [("D_800723B0", ".rodata", 0, 4, 0x11), ("target", "ABS", 0x80001080, 0)],
            relocations=[(".rodata", 0, 2, "target")],
        )
        linked = linked_fixture(self.root / "table.elf", [obj], {".rodata": self.address})
        original = Object(obj).content(Object(obj).section(".rodata"))
        final = Object(linked).content(Object(linked).section(".rodata"))
        self.assertEqual(original, bytes(4))
        self.assertEqual(final, struct.pack(">I", 0x80001080))
        meta = self.project.version("us")
        image = bytearray(meta.baserom.read_bytes())
        image[self.start : self.start + 4] = final
        meta.baserom.write_bytes(image)
        payload = self.payload(bytes_=final)
        payload["rom_sha1"] = hashlib.sha1(image).hexdigest()
        payload["evidence"][0]["final_artifact_sha256"] = hashlib.sha256(linked.read_bytes()).hexdigest()
        payload["evidence"][0]["extents"][0]["rom_bytes_sha256"] = hashlib.sha256(final).hexdigest()
        import toml

        values = toml.load(self.project.root / "config.toml")
        values["version"]["us"]["baserom_sha1"] = payload["rom_sha1"]
        (self.project.root / "config.toml").write_text(toml.dumps(values))
        self.project = config.load(self.project.root)
        record = self.record(payload)
        self.assertEqual(data.coverage(self.project, "us", self.sources, record).manifest["verified_bytes"], 4)
        self.assertNotEqual(payload["evidence"][0]["extents"][0]["bytes_sha256"], hashlib.sha256(original).hexdigest())

    def test_rom_free_current_source_verifier_consumes_the_same_bound_data_proof(self):
        from unbake import buildfiles
        from unbake.report import verify

        for version in self.versions:
            self.record(self.payload(version))
        buildfiles.write_progress(self.project, publish_branch="main")
        with patch.object(progress, "readme_descriptions", return_value={v: f"{v} (fixture)" for v in self.versions}):
            progress.write(self.project, self.host)
        before = (self.project.root / "README.md").read_bytes()
        for version in self.versions:
            self.project.version(version).baserom.unlink()
        with patch.object(process, "run_native") as native, patch.object(process, "run_tool") as tool:
            manifest = verify.validate(self.project)
        native.assert_not_called()
        tool.assert_not_called()
        self.assertEqual((self.project.root / "README.md").read_bytes(), before)
        for version in self.versions:
            self.assertEqual(manifest["versions"][version]["data_coverage"]["verified_bytes"], 16)

    def test_current_inventory_rejects_native_data_event_changed_after_capture(self):
        current = state.inventory(self.project)
        self.record(self.payload())
        with self.assertRaisesRegex(Held, "source.changed"):
            state.assert_current(self.project, current)

    def test_real_rw_all_five_native_objects_have_discarded_constants_no_credit(self):
        for version in self.versions:
            obj = Object(RW / "native" / version / "before-object.elf32")
            final = Object(RW / "native" / version / "before-linked.elf32")
            self.assertTrue(
                any(
                    name in (".rodata", ".data", ".rdata") and row[5]
                    for name, row in zip(obj.names, obj.sections, strict=True)
                )
            )
            self.assertFalse(any(row[1] == 1 and row[2] & 2 and not row[2] & 4 and row[5] for row in final.sections))
            payload = self.payload(version)
            payload["evidence"][0].update(
                final_artifact_sha256=hashlib.sha256(bytes(final.data)).hexdigest(),
                sections=[],
                extents=[],
                unavailable_reason="final .text-only link discards resident source data",
            )
            self.assertEqual(self.cover(payload, version=version).manifest["verified_bytes"], 0)
