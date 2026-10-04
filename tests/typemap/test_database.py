"""Content identity, proven feedback and transactional generated type context."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.decomp.support import fixture
from unbake.decomp import plan
from unbake.layout import index as layout_index
from unbake.layout import split
from unbake.project.config import Held
from unbake.typemap import clear_redraft, context, feedback, feedback_many, load, map_program, redrafts, solve, storage


class DatabaseTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory(dir=os.environ["TMPDIR"])
        self.addCleanup(directory.cleanup)
        self.project, self.policy, _ = fixture(Path(directory.name).resolve(), versions=("us", "eu"), case=self)
        shared = self.project.include[0] / "shared"
        shared.mkdir(exist_ok=True)
        (shared / "typemap.h").write_text("")

    def test_build_extraction_details_do_not_stale_type_inputs(self) -> None:
        build = self.project.build_link("us")
        build.mkdir(parents=True, exist_ok=True)
        table = build / "splat_symbols.csv"
        table.write_text("name,vram_start,size,subsegment_type\ncell,80004000,4,asm\n")
        before = storage.inputs(self.project, headers=True)
        table.write_text("name,vram_start,size,subsegment_type\ncell,80004000,8,c\n")
        self.assertEqual(storage.inputs(self.project, headers=True), before)
        table.write_text("name,vram_start,size,subsegment_type\ncell,80004004,8,c\n")
        self.assertNotEqual(storage.inputs(self.project, headers=True), before)
        table.write_text("name,vram_start,size,subsegment_type\nother,80004000,8,c\n")
        self.assertNotEqual(storage.inputs(self.project, headers=True), before)

    def test_extraction_can_prune_assembly_without_staling_rom_facts(self) -> None:
        before = storage.inputs(self.project, headers=True)
        assembly = self.project.asm / "us" / "obsolete.s"
        assembly.write_text("glabel obsolete\n.word 0x03E00008\n.word 0\n")
        self.assertEqual(storage.inputs(self.project, headers=True), before)
        assembly.unlink()
        self.assertEqual(storage.inputs(self.project, headers=True), before)
        rom = self.project.version("us").baserom
        rom.write_bytes(rom.read_bytes() + b"changed")
        self.assertNotEqual(storage.inputs(self.project, headers=True), before)

    def test_sibling_local_array_view_does_not_erase_published_extern(self) -> None:
        from unbake.typemap import database

        map_program(self.project)
        value = solve(self.project)
        (self.project.src / "alpha.c").write_text("extern void *cell[4]; int alpha(void) { return 1; }")
        value["published_declarations"] = {".published_cell.h": "extern void *cell;"}
        value["published_homes"] = {".published_cell.h": ["main/data.h"]}
        session = database.regeneration.Session(self.project, None)
        outputs = database._render(self.project, value, None, session)
        self.assertIn(b"extern void *cell;", outputs[self.project.include[0] / "main/data.h"])

    def test_map_keeps_all_versions_and_visible_unknown_types(self) -> None:
        mapped = map_program(self.project)
        self.assertEqual(set(mapped["functions"]["alpha"]["versions"]), {"us", "eu"})
        self.assertEqual(len(mapped["functions"]), 3)
        database = solve(self.project)
        self.assertIsNotNone(load(self.project))
        self.assertEqual(database["functions"]["alpha"]["state"], "known")
        self.assertIn("main/alpha.h", context(self.project))
        self.assertNotIn("typedef", (self.project.include[0] / "common/types.h").read_text())

    def test_source_local_contract_is_not_replaced_by_inferred_shared_declaration(self) -> None:
        from unbake.typemap import database

        map_program(self.project)
        value = solve(self.project)
        source = self.project.src / "caller.c"
        source.write_text("extern void alpha(unsigned int arg0);\nvoid caller(void) { alpha(1); }\n")
        value["functions"]["alpha"].update(state="known", prototype="int alpha(int arg0);")
        self.assertIn("src/caller.c", storage.inputs(self.project, headers=True))
        session = database.regeneration.Session(self.project, None)
        outputs = database._render(self.project, value, None, session)
        headers = b"\n".join(data for path, data in outputs.items() if path.suffix == ".h" and isinstance(data, bytes))
        self.assertNotIn(b"extern int alpha(int arg0);", headers)
        self.assertEqual(source.read_text(), "extern void alpha(unsigned int arg0);\nvoid caller(void) { alpha(1); }\n")

    def test_definition_contract_is_not_replaced_by_inferred_owner_prototype(self) -> None:
        from unbake.typemap import database

        map_program(self.project)
        value = solve(self.project)
        source = self.project.src / "alpha.c"
        source.write_text("void alpha(unsigned int arg0) { (void)arg0; }\n")
        value["functions"]["alpha"].update(state="known", prototype="int alpha(int arg0);")
        session = database.regeneration.Session(self.project, None)
        outputs = database._render(self.project, value, None, session)
        headers = b"\n".join(data for path, data in outputs.items() if path.suffix == ".h" and isinstance(data, bytes))
        self.assertNotIn(b"extern int alpha(int arg0);", headers)

    def test_retained_installed_prototype_cannot_contradict_published_definition(self) -> None:
        from unbake.typemap import database

        map_program(self.project)
        value = solve(self.project)
        (self.project.src / "alpha.c").write_text("void alpha(unsigned int arg0) {}\n")
        value["published_declarations"] = {".published-alpha.h": "extern int alpha(int arg0);"}
        session = database.regeneration.Session(self.project, None)
        outputs = database._render(self.project, value, None, session)
        headers = b"\n".join(data for path, data in outputs.items() if path.suffix == ".h" and isinstance(data, bytes))
        self.assertNotIn(b"extern int alpha(int arg0);", headers)

    def test_published_local_struct_is_not_defined_again_in_shared_header(self) -> None:
        from unbake.typemap import database

        map_program(self.project)
        value = solve(self.project)
        (self.project.src / "alpha.c").write_text(
            "struct Local { int word; };\nint alpha(struct Local *p) {return p->word;}\n"
        )
        value["functions"]["alpha"].update(state="known", prototype="int alpha(struct Local *p);")
        value["structs"]["Local"] = {
            "state": "known",
            "generated": True,
            "type": "struct Local",
            "declaration": "struct Local { int word; };",
            "aliases": [],
        }
        session = database.regeneration.Session(self.project, None)
        outputs = database._render(self.project, value, None, session)
        headers = b"\n".join(data for path, data in outputs.items() if path.suffix == ".h" and isinstance(data, bytes))
        self.assertNotIn(b"struct Local { int word; };", headers)
        self.assertNotIn(b"extern int alpha(struct Local *p);", headers)

    def test_required_complete_installed_layout_survives_inference_loss(self) -> None:
        from unbake.typemap import database

        map_program(self.project)
        value = solve(self.project)
        header = self.project.include[0] / "common/types.h"
        storage.write(header, b"struct Retained { int value; };\n")
        lookup = layout_index.load(self.project)
        lookup["headers"]["common/types.h"] = storage.file_digest(header)
        storage.write(layout_index.path(self.project), layout_index.encoded(lookup))
        (self.project.src / "alpha.c").write_text(
            "struct Owner { struct Retained item; };\nint alpha(void) { return 1; }\n"
        )
        (self.project.src / "caller.c").write_text("struct Retained { int other; };\n")
        session = database.regeneration.Session(self.project, None)
        outputs = database._render(self.project, value, None, session)
        headers = b"\n".join(data for path, data in outputs.items() if path.suffix == ".h" and isinstance(data, bytes))
        self.assertIn(b"struct Retained { int value; };", headers)
        self.assertTrue(value["declaration_evidence"])

    def test_version_conditional_local_contracts_are_not_merged_as_shared_conflicts(self) -> None:
        from unbake.typemap import database

        map_program(self.project)
        value = solve(self.project)
        source = self.project.src / "caller.c"
        source.write_text(
            "#if defined(VERSION_EU)\nextern void alpha(unsigned int arg0);\n"
            "#else\nextern int alpha(int arg0);\n#endif\nvoid caller(void) { alpha(1); }\n"
        )
        value["functions"]["alpha"].update(state="known", prototype="int alpha(int arg0);")
        session = database.regeneration.Session(self.project, None)
        outputs = database._render(self.project, value, None, session)
        headers = b"\n".join(data for path, data in outputs.items() if path.suffix == ".h" and isinstance(data, bytes))
        self.assertNotIn(b"extern int alpha(int arg0);", headers)

    def test_shared_typedef_spelling_does_not_hide_an_equivalent_contract(self) -> None:
        from unbake.typemap import database

        shared = self.project.include[0] / "shared/typemap.h"
        shared.write_text("typedef int signed_word;\n")
        map_program(self.project)
        value = solve(self.project)
        (self.project.src / "caller.c").write_text(
            '#include "shared/typemap.h"\nextern signed_word alpha(signed_word arg0);\n'
        )
        value["functions"]["alpha"].update(state="known", prototype="int alpha(int arg0);")
        session = database.regeneration.Session(self.project, None)
        outputs = database._render(self.project, value, None, session)
        headers = b"\n".join(data for path, data in outputs.items() if path.suffix == ".h" and isinstance(data, bytes))
        self.assertIn(b"extern int alpha(int arg0);", headers)

    def test_disassembled_data_never_enters_function_map_or_actionable_pool(self) -> None:
        for version in self.project.versions:
            path = self.project.asm / version / "nonmatchings/alpha.s"
            # These bytes decode as a complete return, but Splat emitted data.
            address = 0x80001000 if version == "us" else 0x80202000
            path.write_text(
                ".section .text\nglabel alpha\n"
                f"/* 40 {address:08X} 24020001 */ .word 0x24020001\n"
                f"/* 44 {address + 4:08X} 03E00008 */ .word 0x03E00008\n"
                f"/* 48 {address + 8:08X} 00000000 */ .word 0\n"
            )
            measured = split.extracted_text(self.project, version, [path])
            self.assertEqual(measured.functions, [])
            self.assertEqual(measured.data, ((64, 68), (68, 72), (72, 76)))
            self.assertNotIn("alpha", split.owners_by_alias(self.project, version))
        mapped = map_program(self.project)
        self.assertNotIn("alpha", mapped["functions"])
        self.assertNotIn("alpha", {row.function for row in plan.actionable(self.project, self.policy)})
        for version in self.project.versions:
            path = self.project.asm / version / "nonmatchings/alpha.s"
            path.write_text(
                path.read_text()
                .replace(".word 0x24020001", "addiu $v0, $zero, 1")
                .replace(".word 0x03E00008", "jr $ra")
                .replace(".word 0", "nop")
            )
        self.assertIn("alpha", map_program(self.project)["functions"])
        self.assertIn("alpha", split.owners_by_alias(self.project, "us"))
        for version in self.project.versions:
            path = self.project.asm / version / "nonmatchings/alpha.s"
            path.write_text(path.read_text().replace("glabel", "dlabel"))
        self.assertNotIn("alpha", map_program(self.project)["functions"])

    def test_solve_repins_header_membership_after_render_and_keeps_summary_current(self) -> None:
        import json

        from unbake.typemap import database

        authored = self.project.include[0] / "provider.h"
        authored.write_text("extern int provided;\n")
        map_program(self.project)
        render = database._render

        def regroup(*args, **kwargs):
            outputs = render(*args, **kwargs)
            outputs[authored] = authored.read_bytes()
            lookup = json.loads(outputs[layout_index.path(self.project)])
            lookup["headers"]["provider.h"] = storage.file_digest(authored)
            outputs[layout_index.path(self.project)] = layout_index.encoded(lookup)
            return outputs

        with patch.object(database, "_render", side_effect=regroup):
            result = solve(self.project)
        self.assertNotIn("include/provider.h", result["inputs_sha256"])
        self.assertEqual(result["inputs_sha256"], storage.inputs(self.project, headers=True))
        self.assertIsNotNone(load(self.project))
        summary = storage.read(self.project.build / "types/summary.json", "types.summary")
        self.assertEqual(summary["database_sha256"], storage.file_digest(self.project.build / "types/database.json"))
        solve(self.project)
        self.assertIsNotNone(load(self.project))

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
            self.project.include[0] / "common/types.h",
            self.project.include[0] / "main/alpha.h",
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
        paths.extend(layout_index.headers(self.project))
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
        project, _, _ = fixture(
            Path(directory.name).resolve(), words=[0x3C088000, 0x8D083000, 0x8D020004, 0x03E00008, 0], case=self
        )
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
        project, _, _ = fixture(
            Path(directory.name).resolve(), words=[0x3C088000, 0x35081018, 0x01000008, 0, 0x03E00008, 0], case=self
        )
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
        project, policy, _ = fixture(Path(directory.name).resolve(), words=words, case=self)
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
        type_header = next(path for path in layout_index.headers(project) if f"struct {shape} {{" in path.read_text())
        text = type_header.read_text().replace("int field_0;", "RetainedWord field_0;")
        type_header.write_text(
            text.replace(
                f"struct {shape} {{",
                f"typedef int RetainedWord;\ntypedef struct {shape} RetainedShape;\nstruct {shape} {{",
            )
        )
        source = project.src / "beta.c"
        source.write_text(
            f'#include "{type_header.relative_to(project.include[0]).as_posix()}"\n'
            f"int beta(struct {shape} *p) {{ return p->field_0; }}\n"
        )
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
        self.assertEqual(result["structs"][shape]["aliases"], ["RetainedShape"])
        self.assertEqual(result["structs"][shape]["typedefs"], {"RetainedWord": "int"})
        self.assertEqual(result["structs"][shape]["common_base"], first["structs"][shape]["common_base"])
        self.assertIsNone(result["structs"][shape]["size"])
        repeated = solve(project, policy)
        self.assertEqual(repeated["structs"][shape]["base_nodes"], result["structs"][shape]["base_nodes"])
        self.assertIsNone(repeated["structs"][shape]["size"])
        shared = "\n".join(path.read_text() for path in layout_index.headers(project))
        self.assertIn(f"struct {shape} {{", shared)
        self.assertIn(f"typedef struct {shape} RetainedShape;", shared)
        self.assertIn("typedef int RetainedWord;", shared)

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
        for row in proven["records"].values():
            retained = self.project.build / "types/sources" / (row["source_sha256"] + ".c")
            self.assertEqual(retained.read_bytes(), (self.project.root / row["source"]).read_bytes())
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
        project, policy, _ = fixture(directory, words=[0x0C000404, 0, 0x03E00008, 0], case=self)
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
