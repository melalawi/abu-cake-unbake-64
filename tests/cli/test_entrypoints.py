"""Process exit status for module and installed command entry points."""

import os
import subprocess
import sys
import sysconfig
from pathlib import Path

from tests.cli.support import MainCase


class EntrypointTests(MainCase):
    def test_held_phases_exit_one_through_real_entrypoints(self) -> None:
        script = Path(sysconfig.get_path("scripts")) / "unbake"
        self.assertTrue(script.is_file(), "Install unbake before running entry point tests")
        environment = dict(
            os.environ,
            PYTHONPATH=str(Path(__file__).resolve().parents[2] / "src"),
            PYTHONNOUSERSITE="1",
        )
        self.source.write_text('void alpha(void) { asm("nop"); }\n')
        cases: tuple[tuple[str, list[str], str], ...] = (
            ("config", [], "phase"),
            ("config", ["setup"], "--project"),
            ("init", ["init"], "DIR"),
            ("setup", ["setup", "--new"], "--new"),
            ("split", ["split"], "verb"),
            ("decomp", ["decomp"], "verb"),
            ("match", ["match"], "verb"),
            ("config", ["report", "--unexpected"], "--unexpected"),
            ("config", ["check", "--unexpected"], "--unexpected"),
            ("init", ["init", "new", "--rompath", str(self.directory / "missing")], "missing directory"),
            ("config", ["--project", str(self.directory / "missing"), "setup"], "config.toml"),
            ("check", self.args("check"), "inline-asm"),
        )
        for entrypoint in ([sys.executable, "-m", "unbake"], [str(script)]):
            for phase, operands, reason in cases:
                with self.subTest(entrypoint=entrypoint, phase=phase, operands=operands):
                    result = subprocess.run(
                        [*entrypoint, *operands],
                        cwd=self.directory,
                        env=environment,
                        capture_output=True,
                        text=True,
                        check=False,
                        timeout=15,
                    )
                    output = result.stdout + result.stderr
                    self.assertEqual(result.returncode, 1, output)
                    self.assertIn(f"HELD({phase}):", output)
                    self.assertIn(reason, output)
                    self.assertNotIn("Traceback", output)
            with self.subTest(entrypoint=entrypoint, operands=["--help"]):
                result = subprocess.run(
                    [*entrypoint, "--help"],
                    cwd=self.directory,
                    env=environment,
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=15,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertNotIn("HELD(", result.stdout + result.stderr)
