"""Installed CLI refuses malformed candidate sets before any work."""

import os
import subprocess
import sysconfig
from dataclasses import asdict
from pathlib import Path

import toml

from tests.cli.support import MainCase


class CompilerTieCliTests(MainCase):
    def test_installed_try_refuses_duplicate_unknown_and_singleton_sets(self):
        path = self.root / "config.toml"
        original = toml.loads(path.read_text())
        policy = self.directory / "policy.toml"
        policy.write_text(
            toml.dumps(
                {key: str(value) if isinstance(value, Path) else value for key, value in asdict(self.policy).items()}
            )
        )
        script = Path(sysconfig.get_path("scripts")) / "unbake"
        environment = dict(os.environ, UNBAKE_POLICY=str(policy), PYTHONNOUSERSITE="1")
        environment.pop("PYTHONPATH", None)
        for ids in (["ido-7.1", "ido-7.1"], ["ido-7.1", "unknown"], ["ido-7.1"]):
            original["compiler_ties"] = {"tie:us:ido": ids}
            original["units"] = {"alpha": "tie:us:ido"}
            path.write_text(toml.dumps(original))
            result = subprocess.run(
                [str(script), *self.args("try", str(self.source))],
                env=environment,
                cwd=self.directory,
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
            output = result.stdout + result.stderr
            self.assertEqual(result.returncode, 1, output)
            self.assertIn("compiler.tied_set", output)
            self.assertNotIn("Traceback", output)
            self.assertEqual(sum(line.startswith("Next:") for line in output.splitlines()), 1)

    def test_try_accepts_exact_equivalence_and_builds_every_member(self):
        import hashlib
        import json
        from contextlib import contextmanager
        from unittest.mock import patch

        from unbake.decomp import trial
        from unbake.decomp.trial_compare import Compare
        from unbake.project import config, makefile

        path = self.root / "config.toml"
        data = toml.loads(path.read_text())
        data["compilers"]["ido-5.3"] = dict(data["compilers"]["ido-7.1"])
        data["compiler_ties"] = {"tie:unit:alpha": ["ido-5.3", "ido-7.1"]}
        data["units"] = {"alpha": "tie:unit:alpha"}
        data["project"]["default_compiler"] = "ido-5.3"
        path.write_text(toml.dumps(data))
        self.source.unlink()
        self.source = self.directory / "alpha.c"
        self.source.write_text("int alpha(void) { return 1; }\n")
        self.project = config.load(self.root)
        self.project.tools.mkdir(exist_ok=True)
        recipe = self.project.tools / "build.json"
        recipe.write_text(json.dumps(makefile.description(self.project)))
        (self.project.tools / "compiler.sha256").write_text(
            hashlib.sha256(recipe.read_bytes()).hexdigest() + "  tools/build.json\n"
        )
        self.project.build.mkdir(exist_ok=True)
        target = self.directory / "target.o"
        target.write_bytes(b"target")
        pinned = {v: (self.directory, target) for v in self.project.versions}
        built = []

        @contextmanager
        def inputs(*args):
            yield pinned

        def compile_candidate(project, policy, source, work, **kwargs):
            built.append(project.compiler_for(source).id)
            words = (0x24020001, 0x03E00008, 0)
            return trial.Trial(
                "alpha",
                hashlib.sha256(source.read_bytes()).hexdigest(),
                {v: Compare(v, 3, 3, {}, [], 100.0, (), target_words=words, candidate_words=words) for v in pinned},
                [],
                "",
            )

        with (
            patch.object(config, "load_policy", return_value=self.policy),
            patch.object(trial, "trial_inputs", side_effect=inputs),
            patch.object(trial, "try_draft", side_effect=compile_candidate),
            patch("unbake.decomp.trial_target.owning_versions", return_value=list(pinned)),
            patch("unbake.decomp.work.identity", return_value={}),
            patch("unbake.decomp.work.persist"),
            patch.object(trial, "store_trial"),
        ):
            code, output, error = self.run_main(self.args("try", str(self.source)), load_project=False)
        self.assertEqual(code, 0, output + error)
        self.assertIn("compiler equivalent alpha", output)
        self.assertEqual(set(built), {"ido-5.3", "ido-7.1"})
        data = toml.loads(path.read_text())
        selection = data["compiler_selections"]["tie:unit:alpha"]
        self.assertEqual(selection["status"], "equivalent")
        self.assertEqual(selection["compiler"], "ido-5.3")
        self.assertIn("region's decided compiler", selection["build_rule"])
        self.assertEqual(json.loads(recipe.read_text())["units"]["alpha"], "ido-5.3")
        for member in json.loads(selection["evidence_json"])["candidates"].values():
            for version in member["versions"].values():
                self.assertEqual(version["candidate_words"], version["target_words"])
