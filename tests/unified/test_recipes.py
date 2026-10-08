"""Real SN64/IDO recipe domains and legacy documents; counterfactual invalid inputs labeled."""

import argparse
import io
from dataclasses import replace
from unittest.mock import patch

import toml

from tests.kit import TempCase
from tests.project_fixture import make
from tests.unified.support import gcc_project, phases, retained
from unbake import buildfiles, config, migrate_state
from unbake.cli import migrate_state as migration_cli
from unbake.cli.args import Context
from unbake.compilers import drivers, options, registry
from unbake.compilers.families import family_for
from unbake.compilers.recipe_options import UnitRecipe, canonical_unit, resolve_options


class RecipeTests(TempCase):
    def test_sn64_precedence_keeps_compiler_g8_and_assembler_g0_and_macro_order(self):
        project, _host, _file = gcc_project(self.root)
        recipe = UnitRecipe.read(
            {"compiler": "gcc-2.8.1-sn64", "options": phases(compile=["-G8"], preprocess=["-UVERSION_US"])}
        )
        view = replace(project, units={"src/func_800B1520_us.c": recipe})
        resolved = drivers.resolved(view, "us", "func_800B1520_us")
        self.assertEqual([t for t in resolved.phase("compile") if t.startswith("-G")], ["-G8"])
        self.assertIn("-G0", resolved.phase("assemble"))
        self.assertLess(
            resolved.phase("preprocess").index("-DVERSION_US"), resolved.phase("preprocess").index("-UVERSION_US")
        )
        self.assertNotEqual(resolved.digest, drivers.resolved(project, "us", "func_800B1520_us").digest)

    def test_default_compiler_flags_survive_generated_units_and_nested_canonical_scope(self):
        project, _host = make(self.root, versions=("us",))
        recipe = UnitRecipe.read({"compiler": "ido-7.1", "options": phases(compile=["-O1"])})
        (project.src / "nested").mkdir()
        (project.src / "nested/alpha.c").write_text("int alpha(void) { return 1; }\n")
        view = replace(project, units={"src/nested/alpha.c": recipe})
        text = buildfiles.units_mk(view)
        self.assertIn("nested/alpha", text)
        self.assertIn("-O1", text)
        self.assertEqual(view.recipe_for("src/nested/alpha.c"), recipe)
        for invalid in ("../alpha.c", "src/../alpha.c", "src//alpha.c", "/src/alpha.c"):
            with self.subTest(counterfactual=invalid), self.assertRaises(ValueError):
                canonical_unit(invalid)

    def test_bound_function_requires_actual_separate_placement_and_nonempty_link_refuses(self):
        recipe = UnitRecipe.read(
            {
                "compiler": "gcc-2.8.1-sn64",
                "options": phases(),
                "functions": {"binding": {"options": phases(compile=["-G8"])}},
            }
        )
        with self.assertRaisesRegex(ValueError, "separately compiled"):
            resolve_options("src/unit.c", recipe, phases(), binding="binding")
        resolved = resolve_options("src/unit.c", recipe, phases(), binding="binding", separately_compiled=True)
        self.assertEqual(resolved.phase("compile"), ("-G8",))
        linked = resolve_options(
            "src/unit.c",
            UnitRecipe.read({"compiler": "gcc-2.8.1-sn64", "options": phases(link=["-Tforeign.ld"])}),
            phases(),
        )
        with self.assertRaisesRegex(ValueError, "bounded placement"):
            family_for("gcc-2.8.1-sn64").validate_options(linked)

    def test_real_fp32_override_preserved_but_unproved_trial_abi_changes_refuse(self):
        project, _host, _file = gcc_project(self.root)
        recipe = UnitRecipe.read({"compiler": "gcc-2.8.1-sn64", "options": phases(compile=["-mfp32"])})
        view = replace(project, units={"src/func_800B1520_us.c": recipe})
        self.assertIn("-mfp32", drivers.resolved(view, "us", "func_800B1520_us").phase("compile"))
        with self.assertRaisesRegex(config.Held, "compatible target/caller"):
            options.admit_trial(project, "func_800B1520_us", recipe)
        self.assertIn("-mfp64", retained("RW-AUDIT-INPUT.md").decode())

    def test_symmetric_release_domains_episode_dedup_and_retained_cse_candidates(self):
        project, host, _file = gcc_project(self.root)
        for ident in ("gcc-2.8.1-sn64", "ido-7.1"):
            space = family_for(ident).option_space(registry.specification(ident))
            self.assertTrue({"small_data", "optimization", "isa", "debug"} <= {s.id for s in space})
        evidence = options.OptionEvidence("retained machine GP", "a" * 64, ("target instruction",), ("-G8",), True)
        episode = options.plan_episode(
            project, "func_800B1520_us", evidence=(evidence,), recipe_limit=99, parent_limit=99
        )
        self.assertLessEqual(len(episode.recipes), 8)
        self.assertEqual(episode.parent_limit, 4)
        digests = [
            drivers.resolved(replace(project, units={"src/func_800B1520_us.c": r}), "us", "func_800B1520_us").digest
            for r in episode.recipes
        ]
        self.assertEqual(len(digests), len(set(digests)))
        spec = registry.specification("gcc-2.8.1-sn64")
        self.assertTrue({"-fno-cse-follow-jumps", "-fno-cse-skip-blocks"} <= set(spec.supported_options))
        self.assertEqual((host.search_frontier, host.episode_recipes, host.episode_parents), (8, 8, 4))

    def test_staged_config_uses_one_document_and_runtime_rejects_legacy(self):
        project, _host = make(self.root)
        original = (project.root / "config.toml").read_text()
        (project.root / "config.toml").write_text(original.replace("schema = 2", "schema = 1"))
        with self.assertRaises(config.Held):
            config.load(project.root)
        loaded = config.load(project.root, text=original)
        self.assertEqual(loaded.default_compiler, "ido-7.1")
        for replacement in ("sn64_asflags = []", "unknown_key = []"):
            with self.subTest(counterfactual=replacement), self.assertRaises(config.Held):
                config.load(project.root, text=original.replace("gnu_asflags = []", replacement))


class MigrationTests(TempCase):
    def legacy(self):
        project, host = make(self.root, versions=("us",))
        path = project.root / "config.toml"
        raw = toml.loads(path.read_text())
        raw["schema"] = 1
        raw["units"] = {"alpha": "ido-7.1"}
        raw["build"]["unit_cflags"] = {"src/alpha.c": ["-O1", "-UVERSION_US"]}
        raw["build"]["sn64_asflags"] = raw["build"].pop("gnu_asflags")
        (project.src / "alpha.c").write_text("int alpha(void) { return 1; }\n")
        project.version("us").split.write_text(
            project.version("us").split.read_text().replace("asm, alpha]", "c, alpha]")
        )
        path.write_text(toml.dumps(raw))
        # Pins are counterfactual byte fixtures at the verify boundary only;
        # parser/resolver/transaction/readback run unchanged.
        spec = registry.specification("ido-7.1")
        pins = {}
        import hashlib

        for name in spec.pins:
            file = project.compiler_for("alpha").cc.parent / name
            file.write_bytes(("mock pin " + name).encode())
            pins[name] = hashlib.sha256(file.read_bytes()).hexdigest()
        project.compiler_for("alpha").cc.parent.rename(project.tools / "ido-7.1")
        self.spec = replace(spec, pins=pins)
        return project, host, path

    def test_raw_cli_migration_combines_stem_compiler_path_flags_before_strict_load(self):
        project, host, path = self.legacy()
        before = path.read_bytes()
        ctx = Context(
            "migrate-state",
            argparse.Namespace(plan=True, apply=None, recover=False, compact=False),
            project.root,
            None,
            io.StringIO(),
            host,
        )
        with (
            patch.object(registry, "specification", return_value=self.spec),
            patch("subprocess.run", side_effect=AssertionError("migration native process")),
        ):
            result = migration_cli.run(ctx)
            planned = result.data
            self.assertEqual(planned["blockers"], [])
            self.assertEqual(set(planned["effective_recipes"]), {"src/alpha.c"})
            self.assertEqual(
                {k: v["options"] for k, v in planned["effective_recipes"].items()}, planned["legacy_effective_recipes"]
            )
            outcome = migrate_state.apply_config(project.root, {k: v for k, v in planned.items() if k != "plan_path"})
            loaded = config.load(project.root)
            self.assertIn("-O1", drivers.resolved(loaded, "us", "alpha").phase("compile"))
            self.assertEqual(outcome["native_calls"], 0)
            self.assertEqual((__import__("pathlib").Path(outcome["backup"]) / "config.toml").read_bytes(), before)

    def test_one_plan_reports_unknown_missing_inputs_and_changed_inputs_refuse_apply(self):
        project, _host, path = self.legacy()
        with patch.object(registry, "specification", return_value=self.spec):
            planned = migrate_state.config_plan(project.root)
            path.write_text(path.read_text() + "\n[unknown]\nvalue = 1\n")
            (project.src / "alpha.c").write_text('#include "missing.h"\nint alpha(void){return 1;}\n')
            blocked = migrate_state.config_plan(project.root)
            self.assertTrue(any("unknown" in s for s in blocked["blockers"]))
            self.assertTrue(any("missing.h" in s for s in blocked["blockers"]))
            with self.assertRaisesRegex(config.Held, "inputs or effective recipes changed"):
                migrate_state.apply_config(project.root, planned)
            self.assertEqual(toml.loads(path.read_text())["schema"], 1)

    def test_real_bt_coexisting_retired_history_is_reported_before_apply_and_preserved(self):
        # Actual paired BT history in a minimal config/pin boundary. No retained
        # native proof is adopted into this counterfactual project identity.
        project, _host, path = self.legacy()
        old = project.root / "attempts.json"
        current = project.root / "attempts.jsonl"
        old.write_bytes(retained("bt-attempts.json"))
        current.write_bytes(retained("bt-attempts.jsonl"))
        before = {p: p.read_bytes() for p in (path, old, current)}
        with (
            patch.object(registry, "specification", return_value=self.spec),
            patch("unbake.migrate_state.Ledger._refresh", side_effect=AssertionError("refuse before history read")),
            patch("subprocess.run", side_effect=AssertionError("migration native process")),
        ):
            planned = migrate_state.config_plan(project.root)
            self.assertTrue(any("current ledger coexists" in b for b in planned["blockers"]))
            with self.assertRaises(config.Held) as held:
                migrate_state.apply_config(project.root, planned)
        self.assertEqual(held.exception.key, "migration.evidence")
        self.assertEqual({p: p.read_bytes() for p in before}, before)
