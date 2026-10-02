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
