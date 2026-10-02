"""Regional pins require complete, strictly separating trial evidence."""

import hashlib
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import toml

from tests.project.test_bootstrap import policy
from unbake.decomp.trial import Trial
from unbake.decomp.trial_compare import Compare
from unbake.decomp.trial_compilers import resolve
from unbake.project import compiler_files, compiler_ties, config, makefile


class CompilerTieTests(unittest.TestCase):
    def setUp(self):
        import shutil

        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        shutil.copytree(Path(__file__).parents[1] / "fixture", self.root, dirs_exist_ok=True)
        path = self.root / "config.toml"
        data = toml.loads(path.read_text())
        data["compilers"]["ido-5.3"] = dict(data["compilers"]["ido-7.1"])
        self.ref = "tie:us:ido"
        data["compiler_ties"] = {self.ref: ["ido-7.1", "ido-5.3"]}
        data["units"] = {"alpha": self.ref, "beta": self.ref}
        path.write_text(toml.dumps(data))
        self.project = config.load(self.root)
        self.policy = policy(self.root)
        self.source = self.root / "alpha.c"
        self.source.write_text("int alpha(void) { return 1; }\n")
        target = self.root / "target.o"
        target.write_bytes(b"target")
        self.pinned = {"us": (self.root, target), "us-rev1": (self.root, target)}
        self.before = path.read_bytes()
        self.recipe = self.project.tools / "build.json"
        self.recipe.parent.mkdir(parents=True, exist_ok=True)
        (self.project.src / "alpha.c").unlink()
        self.recipe.write_text(json.dumps(makefile.description(self.project)))
        self.recipe_before = self.recipe.read_bytes()
        self.manifest = self.project.tools / "compiler.sha256"
        self.manifest.write_text(
            hashlib.sha256(self.recipe_before).hexdigest() + "  tools/build.json\n" + "0" * 64 + "  tools/unchanged\n"
        )
        self.manifest_before = self.manifest.read_bytes()

    def trial(self, project, *args, **kwargs):
        ident = project.compiler_for(self.source).id
        count = 4 if ident == "ido-7.1" else 3
        return Trial("alpha", "hash", {v: Compare(v, count, 4, {}, [], count * 25, ()) for v in self.pinned}, [], "")

    def run_resolve(self, trial=None):
        with (
            patch("unbake.decomp.trial.try_draft", side_effect=trial or self.trial),
            patch("unbake.decomp.trial_target.owning_versions", return_value=list(self.pinned)),
        ):
            return resolve(self.project, self.policy, self.source, self.root, self.pinned)

    def test_unique_match_pins_entire_region_with_evidence(self):
        resolved = self.run_resolve()
        self.assertEqual(resolved.units, {"alpha": "ido-7.1", "beta": "ido-7.1"})
        self.assertEqual(json.loads(self.recipe.read_bytes())["units"], resolved.units)
        self.assertIn(
            hashlib.sha256(self.recipe.read_bytes()).hexdigest() + "  tools/build.json", self.manifest.read_text()
        )
        self.assertIn("0" * 64 + "  tools/unchanged", self.manifest.read_text())
        data = toml.loads((self.root / "config.toml").read_text())
        proof = json.loads(data["compiler_selections"][self.ref]["evidence_json"])
        self.assertEqual(proof["reason"], "unique exact reproduction")
        self.assertEqual(set(proof["candidates"]), {"ido-5.3", "ido-7.1"})
        self.assertEqual(proof["source_sha256"], hashlib.sha256(self.source.read_bytes()).hexdigest())

    def test_independent_candidate_pin_does_not_change_other_items(self):
        path = self.root / "config.toml"
        data = toml.loads(path.read_text())
        data["compiler_ties"] = {
            "tie:unit:alpha": ["ido-5.3", "ido-7.1"],
            "tie:unit:beta": ["ido-5.3", "ido-7.1"],
        }
        data["units"] = {"alpha": "tie:unit:alpha", "beta": "tie:unit:beta"}
        path.write_text(toml.dumps(data))
        self.project = config.load(self.root)
        self.ref = "tie:unit:alpha"
        self.recipe.write_text(json.dumps(makefile.description(self.project)))
        self.manifest.write_text(compiler_files.sha(self.recipe) + "  tools/build.json\n")
        resolved = self.run_resolve()
        self.assertEqual(resolved.units, {"alpha": "ido-7.1", "beta": "tie:unit:beta"})

    def test_strictly_better_nonmatch_pins(self):
        def trial(project, *args, **kwargs):
            result = self.trial(project)
            for comparison in result.compares.values():
                comparison.of = 5
            return result

        self.run_resolve(trial)
        proof = json.loads(
            toml.loads((self.root / "config.toml").read_text())["compiler_selections"][self.ref]["evidence_json"]
        )
        self.assertEqual(proof["reason"], "strictly better measured rank")

    def test_equivalent_comparisons_do_not_pick_order(self):
        def trial(project, *args, **kwargs):
            return self.trial(compiler_ties.candidate(self.project, self.ref, "ido-7.1"))

        with self.assertRaisesRegex(config.Held, "compiler.tie_equivalent"):
            self.run_resolve(trial)
        self.assertEqual((self.root / "config.toml").read_bytes(), self.before)

    def test_compile_failure_is_not_evidence_for_other_candidate(self):
        def trial(project, *args, **kwargs):
            if project.compiler_for(self.source).id == "ido-5.3":
                raise config.Held("try", "compiler error")
            return self.trial(project)

        with self.assertRaisesRegex(config.Held, "compiler.tie_incomplete"):
            self.run_resolve(trial)
        self.assertEqual((self.root / "config.toml").read_bytes(), self.before)

    def test_missing_version_and_config_race_refuse_without_pin(self):
        with (
            patch("unbake.decomp.trial_target.owning_versions", return_value=["us", "us-rev1", "eu"]),
            self.assertRaisesRegex(config.Held, "compiler.tie_versions"),
        ):
            resolve(self.project, self.policy, self.source, self.root, self.pinned)
        with self.assertRaisesRegex(config.Held, "compiler.tie_stale"):
            compiler_ties.pin(self.project, self.ref, "ido-7.1", {}, "changed")
        self.assertEqual((self.root / "config.toml").read_bytes(), self.before)

    def test_pin_recipe_and_config_roll_back_together_on_write_failure(self):
        atomic = compiler_files.atomic_bytes
        calls = 0

        def write(path, content):
            nonlocal calls
            calls += 1
            if calls == 3:
                raise OSError("write failed")
            atomic(path, content)

        with (
            patch.object(compiler_files, "atomic_bytes", side_effect=write),
            self.assertRaisesRegex(OSError, "write failed"),
        ):
            self.run_resolve()
        self.assertEqual((self.root / "config.toml").read_bytes(), self.before)
        self.assertEqual(self.recipe.read_bytes(), self.recipe_before)
        self.assertEqual(self.manifest.read_bytes(), self.manifest_before)

    def test_malformed_sets_are_named(self):
        for ids in ([], ["ido-7.1"], ["ido-7.1", "ido-7.1"], ["ido-7.1", "missing"], "ido-7.1"):
            with self.subTest(ids=ids), self.assertRaisesRegex(config.Held, "compiler.tied_set"):
                compiler_ties.read({self.ref: ids}, self.project.compilers)
        with self.assertRaisesRegex(config.Held, "compiler.tied_set"):
            compiler_ties.read({"bad": ["ido-5.3", "ido-7.1"]}, self.project.compilers)

    def test_segment_tie_uses_same_reference_as_compiler_for(self):
        project = replace(self.project, units={"main": self.ref})
        self.assertEqual(compiler_ties.reference(project, self.source), self.ref)
        self.assertEqual(project.compiler_for(self.source).id, "ido-5.3")
        variant = compiler_ties.candidate(project, self.ref, "ido-7.1")
        self.assertEqual(variant.compiler_for(self.source).id, "ido-7.1")

    def test_default_tie_pin_and_carrier_do_not_mutate_original(self):
        project = replace(self.project, default_compiler=self.ref)
        variant = compiler_ties.candidate(project, self.ref, "ido-7.1")
        self.assertEqual(variant.default_compiler, "ido-7.1")
        self.assertEqual(project.default_compiler, self.ref)
        self.assertEqual(project.compiler_for(self.source).id, "ido-5.3")
        self.assertEqual((self.root / "config.toml").read_bytes(), self.before)
