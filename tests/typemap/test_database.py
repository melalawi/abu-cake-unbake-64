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

    def test_compiler_recipe_rebinding_retains_instruction_shard_and_refuses_layout_changes(self) -> None:
        from unbake.typemap.mapping import compiler_inputs

        mapped = map_program(self.project)
        content = (self.project.root / "config.toml").read_bytes() + b"\n"
        rebound = compiler_inputs(self.project, content)
        self.assertIsNotNone(rebound)
        import json

        result = json.loads(rebound[1])
        self.assertEqual(result["shard_sha256"], mapped["shard_sha256"])
        self.assertEqual(result["inputs_sha256"]["config.toml"], storage.digest(content))
        split = self.project.version("us").split
        split.write_text(split.read_text() + "\n")
        with self.assertRaisesRegex(Held, "map.inputs_stale"):
            compiler_inputs(self.project, content)

    def test_invalid_rendered_header_keeps_previous_revision_and_marks(self) -> None:
        from unbake.typemap.solver import infer

        map_program(self.project)
        solve(self.project)
        paths = [self.project.build / "types" / name for name in ("database.json", "summary.json", "redraft.json")]
        paths.extend(self.project.include[0] / "shared" / name for name in ("typemap.h", "prototypes.h"))
        before = {path: path.read_bytes() for path in paths}

        def malformed(*args, **kwargs):
            result = infer(*args, **kwargs)
            result["functions"]["alpha"].update(state="known", prototype="void alpha(unsigned char[4] arg0);")
            return result

        with (
            patch("unbake.typemap.solver.infer", side_effect=malformed),
            self.assertRaisesRegex(Held, "types.header_parse"),
        ):
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

    def test_symbol_refresh_retains_unaffected_shard_and_adds_unaccessed_names(self) -> None:
        from unbake.typemap.mapping import refresh_map

        before = map_program(self.project)
        symbols = self.project.version("us").symbols
        symbols.write_text(symbols.read_text() + "new_data = 0x80003000;\n")
        with (
            patch("unbake.typemap.mapping.Analysis.run", side_effect=AssertionError("unexpected rescan")),
            patch("unbake.typemap.mapping.indexed_references", side_effect=AssertionError("unchanged index decoded")),
            patch("unbake.typemap.shards.Functions.__getitem__", side_effect=AssertionError("sibling bodies read")),
        ):
            result = refresh_map(self.project)
        self.assertEqual(result["shard_sha256"], before["shard_sha256"])
        self.assertEqual(result["globals"]["new_data"]["versions"], {"us": {"address": 0x80003000}})
        self.assertEqual(result["refresh"]["reused"], 6)
        self.assertEqual(result["refresh"]["rescanned"], 0)
        self.assertFalse(result["refresh"]["abi_upgrade"])
        self.assertEqual(result["refresh"]["previous_shard_sha256"], before["shard_sha256"])

    def test_incremental_refresh_materializes_an_old_abi_supplement_only_once(self) -> None:
        from unbake.typemap.mapping import Analysis, refresh_map

        map_program(self.project)
        path = self.project.build / "map/facts.json"
        manifest = storage.read(path, "map.facts")
        manifest.pop("abi_analysis_sha256")
        storage.write(path, storage.encoded(manifest))
        symbols = self.project.version("us").symbols
        symbols.write_text(symbols.read_text() + "new_data = 0x80003000;\n")
        with patch("unbake.typemap.abi_facts.Analysis.run", autospec=True, side_effect=Analysis.run) as upgraded:
            first = refresh_map(self.project)
        self.assertEqual(upgraded.call_count, 6)
        self.assertTrue(first["refresh"]["abi_upgrade"])
        self.assertEqual(first["refresh"]["rescanned"], 0)
        symbols.write_text(symbols.read_text() + "more_data = 0x80004000;\n")
        with patch("unbake.typemap.mapping.Analysis.run", side_effect=AssertionError("ABI upgraded again")):
            second = refresh_map(self.project)
        self.assertFalse(second["refresh"]["abi_upgrade"])
        self.assertEqual(second["shard_sha256"], first["shard_sha256"])

    def test_boundary_refresh_rescans_only_changed_intervals_and_matches_full_map(self) -> None:
        from unbake.typemap.mapping import Analysis, refresh_map

        map_program(self.project)
        path = self.project.version("us").split
        path.write_text(path.read_text().replace("[0x4C, asm, beta]", "[0x50, asm, beta]"))
        calls = []
        run = Analysis.run

        def tracked(analysis):
            calls.append((analysis.function, analysis.version))
            return run(analysis)

        with patch("unbake.typemap.mapping.Analysis.run", tracked):
            refreshed = refresh_map(self.project)
        self.assertEqual(set(calls), {("alpha", "us"), ("beta", "us")})
        expected = map_program(self.project)
        self.assertEqual(dict(refreshed["functions"]), dict(expected["functions"]))
        self.assertEqual(refreshed["globals"], expected["globals"])

    def test_symbol_origins_refresh_downstream_accesses_on_add_move_and_remove(self) -> None:
        from unbake.typemap.mapping import refresh_map

        directory = tempfile.TemporaryDirectory(dir=os.environ["TMPDIR"])
        self.addCleanup(directory.cleanup)
        project, _, _ = fixture(Path(directory.name), words=[0x3C088000, 0x8D083000, 0x8D020004, 0x03E00008, 0])
        original = map_program(project)
        symbols = project.version("us").symbols
        initial = symbols.read_text()
        for placement in ("source = 0x80003000;\n", "renamed = 0x80003000;\n", "renamed = 0x80004000;\n", ""):
            symbols.write_text(initial + placement)
            refreshed = refresh_map(project)
            expected = map_program(project)
            self.assertEqual(dict(refreshed["functions"]), dict(expected["functions"]))
            self.assertEqual(refreshed["globals"], expected["globals"])
            self.assertLess(refreshed["refresh"]["rescanned"], 3)
        self.assertEqual(original["globals"], expected["globals"])
        with patch("unbake.typemap.mapping.Analysis.run", side_effect=AssertionError("unchanged")):
            refresh_map(project)

    def test_boundary_refresh_updates_constant_indirect_tail_targets(self) -> None:
        from unbake.typemap.mapping import refresh_map

        directory = tempfile.TemporaryDirectory(dir=os.environ["TMPDIR"])
        self.addCleanup(directory.cleanup)
        project, _, _ = fixture(Path(directory.name), words=[0x3C088000, 0x35081018, 0x01000008, 0, 0x03E00008, 0])
        before = map_program(project)
        self.assertEqual(before["functions"]["alpha"]["versions"]["us"]["calls"][0]["callee"], "beta")
        version = project.version("us")
        version.split.write_text(version.split.read_text().replace(", asm, beta]", ", asm, renamed]"))
        version.symbols.write_text(version.symbols.read_text().replace("beta =", "renamed ="))
        refreshed = refresh_map(project)
        self.assertEqual(refreshed["functions"]["alpha"]["versions"]["us"]["calls"][0]["callee"], "renamed")
        expected = map_program(project)
        self.assertEqual(dict(refreshed["functions"]), dict(expected["functions"]))

    def test_failed_incremental_map_keeps_manifest_and_rom_changes_require_bootstrap(self) -> None:
        from unbake.typemap.mapping import refresh_map

        map_program(self.project)
        path = self.project.build / "map/facts.json"
        before = path.read_bytes()
        layout = self.project.version("us").split
        layout.write_text(layout.read_text().replace("[0x4C, asm, beta]", "[0x50, asm, beta]"))
        with (
            patch("unbake.typemap.mapping.Analysis.run", side_effect=ValueError("bad item")),
            self.assertRaisesRegex(ValueError, "bad item"),
        ):
            refresh_map(self.project)
        self.assertEqual(path.read_bytes(), before)
        self.assertFalse(list(path.parent.glob(".facts-*")))
        self.project.version("us").baserom.write_bytes(b"changed")
        with self.assertRaisesRegex(Held, "map.rom_sha1.us"):
            refresh_map(self.project)
        self.assertEqual(path.read_bytes(), before)

    def test_feedback_preserves_a_proven_shared_shape_without_rescanning(self) -> None:
        import hashlib
        from dataclasses import replace

        directory = tempfile.TemporaryDirectory(dir=os.environ["TMPDIR"])
        self.addCleanup(directory.cleanup)
        words = [0x00808021, 0x0C000406, 0x02002021, 0x8E020004, 0x03E00008, 0]
        project, policy, _ = fixture(Path(directory.name), words=words)
        cartridge = project.version("us")
        image = bytearray(cartridge.baserom.read_bytes())
        image[0x58:0x5C] = (0x8C820000).to_bytes(4, "big")
        cartridge.baserom.write_bytes(image)
        project.version_map["us"] = replace(cartridge, baserom_sha1=hashlib.sha1(image).hexdigest())
        mapped = map_program(project)
        first = solve(project)
        shapes = [name for name, row in first["structs"].items() if row["state"] == "known"]
        self.assertEqual(len(shapes), 1)
        shape = shapes[0]
        source = project.src / "beta.c"
        source.write_text('#include "shared/typemap.h"\n' + f"int beta(struct {shape} *p) {{ return p->field_0; }}\n")
        cartridge.split.write_text(cartridge.split.read_text().replace(", asm, beta]", ", c, beta]"))
        proof = {
            "matched": True,
            "source_sha256": storage.file_digest(source),
            "versions": ["us"],
            "target_sha256": {"us": mapped["functions"]["beta"]["versions"]["us"]["target_sha256"]},
        }
        with patch("unbake.typemap.mapping.Analysis.run", side_effect=AssertionError("unexpected rescan")):
            result = feedback(project, "beta", source, versions=["us"], proof=proof, policy=policy)
        self.assertEqual(result["structs"][shape]["state"], "known")
        self.assertTrue(result["structs"][shape]["generated"])
        self.assertEqual(result["structs"][shape]["common_base"], first["structs"][shape]["common_base"])
        self.assertIsNone(result["structs"][shape]["size"])
        repeated = solve(project, policy)
        self.assertEqual(repeated["structs"][shape]["base_nodes"], result["structs"][shape]["base_nodes"])
        self.assertIsNone(repeated["structs"][shape]["size"])
        self.assertIn(f"struct {shape} {{", (project.include[0] / "shared/typemap.h").read_text())

    def test_abi_supplement_retains_map_and_is_content_pinned(self) -> None:
        from unbake.typemap.mapping import Analysis
        from unbake.typemap.solver import solve as solve_

        map_program(self.project)
        path = self.project.build / "map/facts.json"
        manifest = storage.read(path, "map.facts")
        manifest.pop("abi_analysis_sha256")
        storage.write(path, storage.encoded(manifest))
        original = path.read_bytes()
        first = solve_(self.project)
        self.assertEqual(path.read_bytes(), original)
        with patch.object(Analysis, "run", side_effect=AssertionError("unexpected repeat ABI analysis")):
            second = solve_(self.project)
        self.assertEqual(second["abi_supplement"], first["abi_supplement"])
        supplement = self.project.build / "map" / first["abi_supplement"]["path"]
        supplement.write_bytes(b"changed")
        with self.assertRaisesRegex(Held, "map.abi"):
            load(self.project)
        with self.assertRaisesRegex(Held, "map.abi"):
            solve_(self.project)

    def test_failed_abi_refinement_keeps_complete_original_map(self) -> None:
        from unbake.typemap.abi_facts import refine
        from unbake.typemap.mapping import Analysis, load_map

        map_program(self.project)
        path = self.project.build / "map/facts.json"
        manifest = storage.read(path, "map.facts")
        manifest.pop("abi_analysis_sha256")
        storage.write(path, storage.encoded(manifest))
        original = path.read_bytes()
        shard = path.parent / manifest["shard"]
        original_shard = shard.read_bytes()
        run = Analysis.run
        calls = 0

        def analyze(analysis):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise RuntimeError("failed ABI observation")
            return run(analysis)

        with (
            patch.object(Analysis, "run", autospec=True, side_effect=analyze),
            self.assertRaisesRegex(RuntimeError, "failed ABI observation"),
        ):
            refine(self.project, load_map(self.project))
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(shard.read_bytes(), original_shard)
        self.assertFalse(list(path.parent.glob("abi-index-*")))
        self.assertFalse(list(path.parent.glob(".facts-*")))

    def test_batch_feedback_maps_and_solves_once_for_all_exact_receipts(self) -> None:
        from unbake.typemap.mapping import refresh_map as map_
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
            patch("unbake.typemap.mapping.refresh_map", wraps=map_) as mapped_once,
            patch("unbake.typemap.solver.solve", wraps=solve_) as solved_once,
            patch("unbake.typemap.mapping.Analysis.run", side_effect=AssertionError("unexpected rescan")),
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
