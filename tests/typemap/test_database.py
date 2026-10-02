"""Content identity, proven feedback and transactional generated type context."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.decomp.support import fixture
from unbake.project.config import Held
from unbake.typemap import clear_redraft, context, feedback, feedback_many, load, map_program, redrafts, solve, storage


class DatabaseTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory(dir=os.environ["TMPDIR"])
        self.addCleanup(directory.cleanup)
        self.project, self.policy, _ = fixture(Path(directory.name), versions=("us", "eu"))

    def test_map_keeps_all_versions_and_visible_unknown_types(self) -> None:
        mapped = map_program(self.project)
        self.assertEqual(set(mapped["functions"]["alpha"]["versions"]), {"us", "eu"})
        self.assertEqual(len(mapped["functions"]), 3)
        database = solve(self.project)
        self.assertIsNotNone(load(self.project))
        self.assertEqual(database["functions"]["alpha"]["state"], "known")
        self.assertIn("shared/prototypes.h", context(self.project))
        self.assertNotIn("typedef", (self.project.include[0] / "shared/typemap.h").read_text())

    def test_changed_header_and_map_refuse_stale_database(self) -> None:
        map_program(self.project)
        solve(self.project)
        (self.project.include[0] / "types.h").write_text("typedef float s32;\n")
        with self.assertRaisesRegex(Held, "types.inputs_stale"):
            load(self.project)
        self.project.version("us").symbols.write_text("alpha = 0x80001000;\n")
        with self.assertRaisesRegex(Held, "map.inputs_stale"):
            solve(self.project)

    def test_rom_digest_and_missing_symbol_input_are_named(self) -> None:
        self.project.version("eu").baserom.write_bytes(b"changed")
        with self.assertRaisesRegex(Held, "map.rom_sha1.eu"):
            map_program(self.project)

    def test_native_generated_catalog_retains_unaccessed_data_symbols(self) -> None:
        catalog = self.project.build_link("us") / "splat_symbols.csv"
        catalog.write_text("vram_start,name,type\n80003000,unused_data,None\n")
        mapped = map_program(self.project)
        self.assertEqual(mapped["globals"]["unused_data"]["versions"]["us"]["address"], 0x80003000)
        self.assertEqual(mapped["globals"]["unused_data"]["accesses"], [])
        self.assertIn("build/us/splat_symbols.csv", mapped["inputs_sha256"])
        catalog.write_text("vram_start,name,type\n80004000,unused_data,None\n")
        with self.assertRaisesRegex(Held, "map.inputs_stale"):
            solve(self.project)
        self.project.version("eu").symbols.unlink()
        with self.assertRaisesRegex(Held, "map.symbols.eu"):
            map_program(self.project)

    def test_shards_are_lazy_and_pin_every_item_body(self) -> None:
        from unbake.typemap.mapping import load_map
        from unbake.typemap.shards import Functions

        mapped = map_program(self.project)
        self.assertIsInstance(mapped["functions"], Functions)
        manifest = storage.read(self.project.build / "map/facts.json", "map.facts")
        self.assertNotIn("memory", manifest["functions"]["alpha"]["versions"]["us"])
        body = mapped["functions"]["alpha"]["versions"]["us"]
        self.assertIn("memory", body)
        shard = self.project.build / "map" / manifest["shard"]
        shard.write_bytes(b"changed")
        with self.assertRaisesRegex(Held, "map.shards"):
            load_map(self.project)

    def test_monolithic_map_is_refused_before_reading(self) -> None:
        from unbake.typemap.mapping import load_map

        path = self.project.build / "map/facts.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("wb") as stream:
            stream.truncate(65 * 1024 * 1024)
        with patch("unbake.typemap.storage.read") as read:
            with self.assertRaisesRegex(Held, "compact sharded map required"):
                load_map(self.project)
            read.assert_not_called()

    def test_failed_item_map_preserves_previous_manifest_and_cleans_temporary(self) -> None:
        map_program(self.project)
        path = self.project.build / "map/facts.json"
        before = path.read_bytes()
        with (
            patch("unbake.typemap.mapping.Analysis.run", side_effect=ValueError("bad item")),
            self.assertRaisesRegex(ValueError, "bad item"),
        ):
            map_program(self.project)
        self.assertEqual(path.read_bytes(), before)
        self.assertFalse(list(path.parent.glob(".facts-*")))

    def test_solve_marks_changed_function_and_digest_clear_rejects_race(self) -> None:
        map_program(self.project)
        solve(self.project)
        (self.project.include[0] / "signatures.h").write_text("unsigned int alpha(void);\n")
        solve(self.project)
        marks = redrafts(self.project)
        self.assertIn("alpha", marks)
        digest = storage.digest((self.project.build / "types/database.json").read_bytes())
        with self.assertRaisesRegex(Held, "types.redraft"):
            clear_redraft(self.project, "alpha", "0" * 64)
        clear_redraft(self.project, "alpha", digest)
        self.assertNotIn("alpha", redrafts(self.project))

    def test_repeated_solve_reads_semantic_summary_without_decoding_database(self) -> None:
        map_program(self.project)
        first = solve(self.project)
        database = self.project.build / "types/database.json"
        read = storage.read

        def bounded(path: Path, key: str) -> dict:
            self.assertNotEqual(path, database)
            return read(path, key)

        with patch("unbake.typemap.storage.read", side_effect=bounded):
            second = solve(self.project)
        self.assertEqual(second["functions"], first["functions"])
        self.assertEqual(second["revision"], first["revision"] + 1)
        self.assertEqual(redrafts(self.project), {})

    def test_summary_refuses_an_independently_changed_database(self) -> None:
        map_program(self.project)
        solve(self.project)
        path = self.project.build / "types/database.json"
        path.write_bytes(path.read_bytes() + b"\n")
        with self.assertRaisesRegex(Held, "types.summary"):
            solve(self.project)

    def test_diagnostic_constraint_shard_is_content_pinned_and_complete(self) -> None:
        import json

        map_program(self.project)
        result = solve(self.project)
        shard = result["constraints"][0]
        self.assertEqual(shard["kind"], "shard")
        path = self.project.root / shard["path"]
        self.assertEqual(shard["sha256"], storage.file_digest(path))
        rows = [json.loads(line) for line in path.read_text().splitlines()]
        self.assertEqual(shard["count"], len(rows))
        self.assertTrue(all(row["kind"] in ("access", "value_flow") for row in rows))
        load(self.project)
        path.write_text("changed")
        with self.assertRaisesRegex(Held, "types.constraints"):
            load(self.project)

    def test_generated_header_failure_rolls_back_every_file(self) -> None:
        map_program(self.project)
        solve(self.project)
        paths = [
            self.project.build / "types/database.json",
            self.project.include[0] / "shared/typemap.h",
            self.project.include[0] / "shared/prototypes.h",
            self.project.build / "types/redraft.json",
            self.project.build / "types/summary.json",
        ]
        before = {path: path.read_bytes() for path in paths}
        install = storage.install

        def fail(path: Path, staged: Path) -> None:
            if path == paths[0] and staged.read_bytes() != before[path]:
                raise OSError("injected database failure")
            install(path, staged)

        with patch("unbake.typemap.storage.install", side_effect=fail), self.assertRaisesRegex(OSError, "injected"):
            solve(self.project)
        self.assertEqual(before, {path: path.read_bytes() for path in paths})

    def test_feedback_refuses_fuzzy_and_unpublished_sources(self) -> None:
        source = self.project.src / "alpha.c"
        source.write_text("int alpha(void) { return 1; }\n")
        with self.assertRaisesRegex(Held, "types.feedback.matched"):
            feedback(self.project, "alpha", source, versions=["us", "eu"], proof={})
        mapped = map_program(self.project)
        proof = {
            "matched": True,
            "source_sha256": storage.digest(source.read_bytes()),
            "versions": ["us", "eu"],
            "target_sha256": {v: row["target_sha256"] for v, row in mapped["functions"]["alpha"]["versions"].items()},
        }
        with self.assertRaisesRegex(Held, "types.feedback.matched"):
            feedback(self.project, "alpha", source, versions=["us", "eu"], proof=proof)
        self.assertFalse((self.project.build / "types/proven.json").exists())

    def test_batch_feedback_maps_and_solves_once_for_all_exact_receipts(self) -> None:
        from unbake.typemap.mapping import map_program as map_
        from unbake.typemap.solver import solve as solve_

        entries = []
        for function in ("alpha", "beta"):
            source = self.project.src / (function + ".c")
            source.write_text(f"int {function}(void) {{ return 1; }}\n")
            for version in self.project.versions:
                split = self.project.version(version).split
                split.write_text(
                    split.read_text()
                    .replace(f", asm, {function}]", f", c, {function}]")
                    .replace(f", asm, nonmatchings/{function}]", f", c, {function}]")
                )
            entries.append({"function": function, "source": source, "versions": list(self.project.versions)})
        mapped = map_program(self.project)
        for entry in entries:
            entry["proof"] = {
                "matched": True,
                "source_sha256": storage.file_digest(entry["source"]),
                "versions": entry["versions"],
                "target_sha256": {
                    v: row["target_sha256"] for v, row in mapped["functions"][entry["function"]]["versions"].items()
                },
            }
        with (
            patch("unbake.typemap.mapping.map_program", wraps=map_) as mapped_once,
            patch("unbake.typemap.solver.solve", wraps=solve_) as solved_once,
        ):
            result = feedback_many(self.project, entries, policy=self.policy)
        self.assertEqual(mapped_once.call_count, 1)
        self.assertEqual(solved_once.call_count, 1)
        proven = storage.read(self.project.build / "types/proven.json", "types.feedback")
        self.assertEqual(set(proven["records"]), {"alpha", "beta"})
        for function in ("alpha", "beta"):
            self.assertEqual(result["functions"][function]["state"], "known")
            self.assertEqual(result["functions"][function]["provenance"][0]["kind"], "proven")
        invalid = [
            {**entry, "proof": {**entry["proof"], "target_sha256": {"us": "bad", "eu": "bad"}}}
            if entry["function"] == "beta"
            else entry
            for entry in entries
        ]
        before = (self.project.build / "types/proven.json").read_bytes()
        with self.assertRaisesRegex(Held, "types.feedback.target_sha256"):
            feedback_many(self.project, invalid, policy=self.policy)
        self.assertEqual(before, (self.project.build / "types/proven.json").read_bytes())

    def test_proven_feedback_resolves_signature_and_marks_calling_neighbour(self) -> None:
        # Alpha's delay slot leaves its incoming argument available to beta.
        directory = Path(self.project.root).parent / "other"
        directory.mkdir()
        project, policy, _ = fixture(directory, words=[0x0C000404, 0, 0x03E00008, 0])
        map_program(project)
        solve(project)
        source = project.src / "beta.c"
        source.write_text("int beta(int value) { return value; }\n")
        split = project.version("us").split
        split.write_text(split.read_text().replace(", asm, beta]", ", c, beta]"))
        mapped = map_program(project)
        proof = {
            "matched": True,
            "source_sha256": storage.digest(source.read_bytes()),
            "versions": ["us"],
            "target_sha256": {"us": mapped["functions"]["beta"]["versions"]["us"]["target_sha256"]},
        }
        result = feedback(project, "beta", source, versions=["us"], proof=proof, policy=policy)
        self.assertEqual(result["functions"]["beta"]["state"], "known")
        self.assertIn("alpha", redrafts(project))
        self.assertEqual(result["functions"]["beta"]["provenance"][0]["kind"], "proven")


if __name__ == "__main__":
    unittest.main()
