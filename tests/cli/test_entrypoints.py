"""Process exit status for module and installed command entry points."""

from tests.cli.support import MainCase


class EntrypointTests(MainCase):
    def test_held_phases_exit_one_through_real_entrypoints(self) -> None:
        self.source.write_text('void alpha(void) { asm("nop"); }\n')
        cases: tuple[tuple[str, list[str], str], ...] = (
            ("config", [], "phase"),
            ("config", ["setup"], "project.root"),
            ("init", ["init"], "init.target"),
            ("config", ["setup", "--new"], "--new"),
            ("split", ["split"], "verb"),
            ("decomp", ["decomp"], "verb"),
            ("config", ["match"], "invalid choice: 'match'"),
            ("config", ["report", "--unexpected"], "--unexpected"),
            ("config", ["check", "--unexpected"], "--unexpected"),
            ("config", ["init", "new", "--rompath", str(self.directory / "missing")], "--rompath"),
            ("config", ["--project", str(self.directory / "missing"), "setup"], "config.toml"),
            ("check", self.args("check"), "inline-asm"),
        )
        from tests.process_fakes import cli

        for phase, operands, reason in cases:
            with self.subTest(phase=phase, operands=operands):
                result = cli(operands, cwd=self.directory)
                output = result.stdout + result.stderr
                self.assertEqual(result.returncode, 1, output)
                self.assertIn(f"HELD({phase}):", output)
                self.assertIn(reason, output)
                self.assertNotIn("Traceback", output)
        result = cli(["--help"], cwd=self.directory)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("HELD(", result.stdout + result.stderr)
