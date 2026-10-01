from pathlib import Path
from unittest.mock import Mock, patch

from tests.cli.support import MainCase
from unbake.project import config


class InitSetupTests(MainCase):
    def test_init_dispatch_passes_explicit_overrides_without_project(self) -> None:
        from unbake.project import init

        run = Mock(return_value=["created"])
        module = self.module("init", run=run)
        code, _out, error = self.run_main(
            [
                "init",
                "new",
                "--rom",
                "input.z64",
                "--compiler",
                "main=ido-7.1",
                "--version-name",
                "eu-x=eu-test",
                "--names-from",
                "us",
                "--name",
                "game",
                "--title",
                "Game",
                "--split",
                "files",
                "--supply",
                "supplied",
            ],
            {"init": module},
        )
        self.assertEqual(code, 0, error)
        target, roms, forced = run.call_args.args
        self.assertEqual((target, roms), (Path("new"), [Path("input.z64")]))
        self.assertIsInstance(forced, init.Forced)
        self.assertEqual(forced.names_from, "us")
        self.assertEqual(forced.compiler, {"main": "ido-7.1"})
        self.assertEqual(forced.version_names, {"eu-x": "eu-test"})
        self.assertEqual(forced.supply, Path("supplied"))

    def test_setup_dispatch_passes_new_rom_and_receipts(self) -> None:
        run = Mock(return_value=["verified us", "OK(setup): verified us-rev1"])
        code, out, error = self.run_main(
            self.args("setup", "--new", "rom.z64"), {"setup": self.module("setup", run=run)}
        )
        run.assert_called_once_with(self.project, self.policy, new_rom=Path("rom.z64"))
        self.assertEqual(code, 0)
        self.assertEqual(out, "OK(setup): verified us\nOK(setup): verified us-rev1\n")
        self.assertEqual(error, "")

    def test_init_input_selection_and_named_refusals(self) -> None:
        inputs = self.directory / "roms"
        inputs.mkdir()
        first = inputs / "first.z64"
        first.write_bytes(b"not a ROM")
        (inputs / "subdirectory").mkdir()
        with patch("unbake.project.init.run", autospec=True, return_value=[]) as run:
            code, _, error = self.run_main(["init", "new", "--rompath", str(inputs)])
            self.assertEqual(code, 0, error)
            self.assertEqual(run.call_args.args[1], [first])
        cases = [
            (["init", "new"], "--rompath"),
            (["init", "new", "--rompath", str(inputs), "--rom", str(first)], "--rom"),
            (["init", "new", "--rompath", str(inputs / "absent")], "--rompath"),
            (["init", str(self.directory / "new"), "--rompath", str(inputs)], "first.z64"),
            (["init", "new", "--rom", str(first), "--default", "us"], "--default"),
        ]
        first.unlink()
        cases.insert(3, (["init", "new", "--rompath", str(inputs)], "empty directory"))
        for operands, name in cases:
            with self.subTest(name=name):
                if name == "first.z64":
                    first.write_bytes(b"not a ROM")
                code, _, error = self.run_main(operands)
                self.assertEqual(code, 1)
                self.assertIn(name, error)

    def test_setup_refusal_keeps_original_phase_and_reason(self) -> None:
        run = Mock(side_effect=config.Held("setup", "compiler.sha256 missing"))
        code, out, error = self.run_main(self.args("setup"), {"setup": self.module("setup", run=run)})
        self.assertEqual(code, 1)
        self.assertEqual(error, "HELD(setup): compiler.sha256 missing\n")
        self.assertEqual(out, "")
