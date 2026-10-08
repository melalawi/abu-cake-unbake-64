"""Accepted standalone producer evidence using the captured SN64 records and symbolic RSP boot."""

import copy
import gzip
import hashlib
import os
from pathlib import Path
from unittest.mock import patch

import toml

from tests.test_resource_build import ResourceBuildTests
from tests.test_source_data_build import FIXTURE, RECORDS, SourceDataBuildTests
from unbake import config, process
from unbake.config import Held
from unbake.report import data, progress, state
from unbake.work import attempts


def prepare(case):
    project = case.project
    for relative in data.required(project, "us"):
        path = project.root / relative
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("native fixture recipe\n")
    values = toml.load(project.root / "config.toml")
    values["version"]["us"]["baserom_sha1"] = hashlib.sha1(project.version("us").baserom.read_bytes()).hexdigest()
    (project.root / "config.toml").write_text(toml.dumps(values))
    case.project = config.load(project.root)
    return case.project


def records(case):
    project = prepare(case)
    (project.root / "versions/us/fixture.data.ld").write_text("SECTIONS { .data : { *(.rodata) } }\n")
    for row in RECORDS:
        path = project.build_link("us") / "data" / row["symbol"]
        path.with_suffix(".elf").write_bytes(gzip.decompress((FIXTURE / (row["symbol"] + ".elf.gz")).read_bytes()))
        path.with_suffix(".bin").write_bytes(gzip.decompress((FIXTURE / (row["symbol"] + ".bin.gz")).read_bytes()))
    return project


class StandaloneDataProgressTests(SourceDataBuildTests):
    def test_actual_33253_record_bytes_are_persisted_independently_and_exported_rom_free(self):
        project = records(self)
        with patch.object(process, "run_native") as native:
            accepted = self.admit()
            self.assertEqual(len(accepted["native_data"]), 2)
            events = data.record_producers(project, accepted["native_data"])
            self.assertEqual(len(events), 2)
            self.assertEqual(data.record_producers(project, accepted["native_data"]), [])
            current = state.inventory(project)
            report = progress.measure(project, None, "us", current=current)
            self.assertEqual(report["measures"]["matched_data"], 33253)
            self.assertEqual(report["measures"]["complete_data"], 33253)
            self.assertEqual(report["measures"]["matched_code"], 0)
            owned = [unit for unit in report["units"] if unit["metadata"].get("source_path")]
            self.assertEqual(len(owned), 2)
            for unit in owned:
                self.assertTrue(unit["metadata"]["complete"])
                self.assertEqual(unit["measures"]["complete_units"], 1)
                self.assertEqual(unit["sections"][0]["fuzzy_match_percent"], 100.0)
                self.assertEqual(unit["measures"]["total_data"], unit["measures"]["matched_data"])
            self.assertEqual(
                next(cat for cat in report["categories"] if cat["id"] == "data")["measures"]["matched_data"], 33253
            )
            history = (project.root / attempts.PATH).read_bytes()
            # Unrelated inventory/recipe rows do not invalidate or replicate every producer.
            split_path = project.version("us").split
            split_path.write_text(split_path.read_text().replace("unclaimed", "unrelated_raw"))
            (project.root / "units.mk").write_text(
                (project.root / "units.mk").read_text() + "# another source recipe\n"
            )
            self.assertEqual(progress.measure(project, None, "us")["measures"]["matched_data"], 33253)
            self.changed = set()
            self.assertEqual(self.admit()["native_data"], [])
            project.version("us").baserom.unlink()
            self.assertEqual(progress.measure(project, None, "us")["measures"], report["measures"])
            self.assertEqual((project.root / attempts.PATH).read_bytes(), history)
        native.assert_not_called()

    def test_actual_stale_source_header_missing_native_and_missing_proof_do_not_receive_credit(self):
        project = records(self)
        self.assertEqual(progress.measure(project, None, "us")["measures"]["matched_data"], 0)
        accepted = self.admit()
        data.record_producers(project, accepted["native_data"])
        source = project.src / (RECORDS[0]["symbol"] + ".c")
        source.write_bytes(source.read_bytes() + b"\n")
        self.assertEqual(progress.measure(project, None, "us")["measures"]["matched_data"], 12792)
        # An old output cannot mint a new proof for this edited source.
        self.assertEqual(len(self.admit()["native_data"]), 1)
        header = project.include[-1] / "sn64_type_records.h"
        header.write_text(header.read_text() + "\n")
        self.assertEqual(progress.measure(project, None, "us")["measures"]["matched_data"], 0)
        path = project.build_link("us") / "data" / (RECORDS[0]["symbol"] + ".bin")
        path.unlink()
        with self.assertRaises(Held):
            self.admit()

    def test_actual_overlapping_current_extents_and_raw_assembly_are_rejected(self):
        project = records(self)
        proof = self.admit()["native_data"][0]
        damaged = copy.deepcopy(proof)
        extent = damaged["payload"]["evidence"][0]["extents"][0]
        other = copy.deepcopy(extent)
        other.update(rom_offset=extent["rom_offset"] + 4, address=extent["address"] + 4, size=8)
        damaged["payload"]["evidence"][0]["extents"].append(other)
        data.record_producers(project, [damaged])
        with self.assertRaisesRegex(Held, "overlapping"):
            progress.measure(project, None, "us")
        # A raw inventory row never creates a producer event or a source binding.
        meta = project.version("us")
        meta.split.write_text(meta.split.read_text().replace('"src/' + RECORDS[0]["symbol"] + '.c"', "raw"))
        self.assertEqual(progress.measure(project, None, "us")["measures"]["matched_data"], 0)


class StandaloneResourceProgressTests(ResourceBuildTests):
    def setup_producer(self):
        project = prepare(self)
        path = project.root / "versions/us/resources/rsp_boot.ld"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes((Path(__file__).parent / "fixtures/resource_boot/boot.ld").read_bytes())
        self.native.write_bytes((Path(__file__).parent / "fixtures/resource_boot/boot.bin").read_bytes())
        return project

    def test_actual_symbolic_boot_storage_credit_is_data_resource_with_execution_binding_and_currentness(self):
        project = self.setup_producer()
        with patch.object(process, "run_native") as native:
            accepted = self.admit()
            self.assertEqual(len(accepted["native_data"]), 1)
            data.record_producers(project, accepted["native_data"])
            report = progress.measure(project, None, "us")
            self.assertEqual(
                (
                    report["measures"]["total_data"],
                    report["measures"]["matched_data"],
                    report["measures"]["complete_data"],
                ),
                (208, 208, 208),
            )
            self.assertEqual(
                (
                    report["measures"]["total_code"],
                    report["measures"]["matched_code"],
                    report["measures"]["total_functions"],
                ),
                (0, 0, 0),
            )
            unit = report["units"][0]
            self.assertEqual(
                unit["metadata"],
                {"complete": True, "source_path": "resources/rsp/boot.s", "progress_categories": ["data", "resource"]},
            )
            self.assertEqual(
                next(cat for cat in report["categories"] if cat["id"] == "resource")["measures"]["matched_data"], 208
            )
            before = (project.root / attempts.PATH).read_bytes()
            self.source.write_text(self.source.read_text() + "\n")
            self.assertEqual(progress.measure(project, None, "us")["measures"]["matched_data"], 0)
            self.assertEqual(self.admit()["native_data"], [])
            self.assertEqual((project.root / attempts.PATH).read_bytes(), before)
        native.assert_not_called()

    def test_actual_missing_stale_and_raw_resource_remain_unmeasured_with_no_extra_gate(self):
        project = self.setup_producer()
        stamp = self.source.stat().st_mtime_ns
        os.utime(self.native, ns=(stamp - 1, stamp - 1))
        self.assertEqual(self.admit()["native_data"], [])
        self.assertEqual(progress.measure(project, None, "us")["measures"]["matched_data"], 0)
        self.native.write_bytes((Path(__file__).parent / "fixtures/resource_boot/boot.bin").read_bytes())
        self.source.write_text(self.source.read_text() + '\n.incbin "boot.bin"\n')
        with self.assertRaises(Held):
            self.admit()
        self.native.unlink()
        self.source.write_bytes((Path(__file__).parent / "fixtures/resource_boot/boot.s").read_bytes())
        with self.assertRaises(Held):
            self.admit()
