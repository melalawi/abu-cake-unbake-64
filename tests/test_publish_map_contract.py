"""Reject real old map contracts before entering any per-file publication work."""

import io
import json
from contextlib import nullcontext, redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from tests.project_fixture import ProjectCase
from unbake import admission, config, land, process, steps, strict_json
from unbake.cli import main
from unbake.typemap import mapping, shards, storage

FIXTURE = Path(__file__).parent / "fixtures/map_contract/legacy_manifest.json"


class PublishMapContractTests(ProjectCase):
    def manifest(self, **changes):
        value = {
            **json.loads(FIXTURE.read_text()),
            **storage.identity(self.project),
            "map_schema": mapping.SCHEMA - 1,
            **changes,
        }
        file = self.project.build / "map/facts.json"
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(json.dumps(value))
        return file

    def invoke(self):
        files = []
        for index in range(16):
            source = self.project.work / str(index) / "alpha.c"
            source.parent.mkdir(parents=True)
            source.write_text("int alpha(void) { return 1; }\n")
            files.append(source)
        out, err = io.StringIO(), io.StringIO()
        with (
            redirect_stdout(out),
            redirect_stderr(err),
            patch.object(config, "load_host", return_value=self.host),
            patch.object(admission, "command", return_value=nullcontext()),
            patch.object(land, "land", return_value="fixture-commit") as publish,
            patch.object(land.compare, "compare") as compare,
            patch.object(steps, "ensure") as maintained,
            patch.object(process, "run_native") as native,
            patch.object(strict_json, "read", wraps=strict_json.read) as read,
        ):
            main.main(["--project", str(self.project.root), "publish", "--fuzzy", "--compare", *map(str, files)])
        return json.loads(out.getvalue()), (
            sum(call.args[0] == self.project.build / "map/facts.json" for call in read.call_args_list),
            compare.call_count,
            publish.call_count,
            maintained.call_count,
            native.call_count,
        )

    def test_public_sixteen_items_wrong_schema_reads_manifest_once_and_runs_nothing(self):
        self.manifest()
        result, counts = self.invoke()
        self.assertEqual(counts, (1, 0, 0, 0, 0))
        self.assertEqual((result["status"], result["key"]), ("held", "map.schema"))
        self.assertEqual(result["data"]["fault"]["cause"]["action"]["argv"], ["recompute", "rom-facts"])
        self.assertIn("recompute rom-facts", result["next"])
        self.assertFalse((self.project.root / "attempts.jsonl").exists())

    def test_version_inventory_mismatch_stops_before_compare_and_publish(self):
        self.manifest(map_schema=mapping.SCHEMA, rom_sha1={"missing-version": "0" * 40})
        result, counts = self.invoke()
        self.assertEqual(counts, (1, 0, 0, 0, 0))
        self.assertEqual(result["key"], "map.facts")
        self.assertIn("recompute rom-facts", result["next"])

    def test_map_reader_checks_schema_before_shard_hash_and_inventory(self):
        self.manifest(shard="unavailable.sqlite", shard_sha256="0" * 64, functions={})
        with (
            patch.object(mapping.inputs, "digest") as digest,
            patch.object(shards, "validate_inventory") as scan,
            self.assertRaises(config.Held) as caught,
        ):
            mapping.load_map(self.project)
        self.assertEqual((digest.call_count, scan.call_count), (0, 0))
        self.assertEqual(caught.exception.key, "map.schema")

    def test_valid_contract_reaches_compare_and_publish_without_shard_open(self):
        self.manifest(map_schema=mapping.SCHEMA)
        result, counts = self.invoke()
        self.assertEqual(counts, (1, 16, 16, 0, 0))
        self.assertEqual(result["status"], "ok")

    def test_strict_contract_rejects_bool_duplicate_and_missing_version_fields(self):
        file = self.manifest(map_schema=mapping.SCHEMA)
        valid = file.read_text()
        invalid = [
            valid.replace('"map_schema": 6', '"map_schema": true'),
            valid.replace('"schema": 1', '"schema": true'),
            valid.replace('"map_schema": 6', '"map_schema": 6, "map_schema": 6'),
        ]
        for text in invalid:
            file.write_text(text)
            with self.assertRaises(config.Held):
                mapping.validate_contract(self.project)

    def test_absent_disposable_map_does_not_claim_a_contract_or_start_regeneration(self):
        with patch.object(mapping, "map_program") as regenerate:
            self.assertIsNone(mapping.validate_contract(self.project))
        regenerate.assert_not_called()
