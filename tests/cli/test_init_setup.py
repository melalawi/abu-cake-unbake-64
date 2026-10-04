"""Shell setup dispatch, retired arguments and terminal guidance."""

import hashlib
from unittest.mock import Mock, patch

from tests.cli.support import MainCase
from unbake.cli.guidance import command
from unbake.cli.setup import pairs
from unbake import config
from unbake.config import Held


class InitSetupTests(MainCase):
    def setUp(self):
        super().setUp()
        from tests.rom_fixture import install

        install(self)

    def test_init_bypasses_project_and_policy(self) -> None:
        target = self.directory / "unrelated name"
        with patch.object(config, "load") as load, patch.object(config, "load_policy") as policy:
            code, out, error = self.run_main(["init", str(target), "--layout-cap", "2"], load_project=False)
        self.assertEqual(code, 0, error)
        load.assert_not_called()
        policy.assert_not_called()
        self.assertIn(str(target / "roms"), out)
        self.assertEqual(config.load_pending(target).state, "awaiting-roms")

    def test_empty_roms_refuses_setup_roms_before_policy(self) -> None:
        target = self.directory / "shell"
        self.run_main(["init", str(target), "--layout-cap", "2"], load_project=False)
        before = hashlib.sha256((target / "config.toml").read_bytes()).hexdigest()
        with patch.object(config, "load_policy") as policy:
            code, out, error = self.run_main(["--project", str(target), "setup"], load_project=False)
        self.assertEqual(code, 1)
        self.assertIn("HELD(setup): setup.roms", out)
        self.assertIn(f"Next: Put ROMs in {target / 'roms'}. Then run ", out)
        self.assertEqual(error, "")
        policy.assert_not_called()
        self.assertEqual(hashlib.sha256((target / "config.toml").read_bytes()).hexdigest(), before)
        self.assertFalse((target / "build").exists())

    def test_retired_init_and_setup_routes_refuse(self) -> None:
        for args in (
            ["init", "new", "--layout-cap", "2", "--rom", "dump.z64"],
            ["init", "new", "--layout-cap", "2", "--split", "files"],
            ["init", "new", "--layout-cap", "2", "--compiler", "ido-7.1"],
            ["setup", "--new", "dump.z64"],
        ):
            with self.subTest(args=args):
                code, out, error = self.run_main(args)
                self.assertEqual(code, 1)
                self.assertIn("unrecognized arguments", out)
                self.assertEqual(error, "")

    def test_setup_dispatch_accepts_pending_config(self) -> None:
        from unbake.project import init

        target = self.directory / "shell"
        init.run(target, layout_cap=2)
        run = Mock(return_value=False)
        with patch("unbake.cli.setup.run", run):
            code, _out, error = self.run_main(
                ["--project", str(target), "setup", "--names-from", "us"], load_project=False
            )
        self.assertEqual(code, 0, error)
        args, pending = run.call_args.args
        self.assertEqual(args.names_from, "us")
        self.assertEqual(pending.state, "awaiting-roms")

    def test_setup_version_rename_assignments_are_explicit(self) -> None:
        self.assertEqual(pairs(["eu-x=pal"], "--version-name"), {"eu-x": "pal"})
        for values in (["us"], ["=pal"], ["us="], ["us=a", "us=b"]):
            with self.subTest(values=values), self.assertRaises(Held):
                pairs(values, "--version-name")

    def test_name_refusal_restart_reuses_the_explicit_census_choice(self) -> None:
        from tests.project.test_rom import BOOTCODES, CODE, cartridge
        from unbake.project import census, flow, header, init
        from unbake.config import Unfinished

        target = self.directory / "unrelated name"
        init.run(target, layout_cap=2)
        (target / "roms/input").write_bytes(cartridge())
        ranges = (type("Range", (), {"start": 0x1000, "end": 0x1000 + len(CODE), "address": 0x80001000})(),)
        with (
            patch.object(header, "RETAIL", BOOTCODES),
            patch.object(census, "measured_code", return_value=ranges),
            patch.object(config, "load_policy", return_value=self.policy),
            patch.object(flow, "plan_layout", side_effect=Unfinished("setup", "layout.plan")),
        ):
            code, out, _error = self.run_main(
                ["--project", str(target), "setup", "--names-from", "us"], load_project=False
            )
            self.assertEqual(code, 1)
            self.assertIn("project.name", out)
            code, out, _error = self.run_main(["--project", str(target), "setup", "--name", "pot"], load_project=False)
        self.assertEqual(code, 1)
        self.assertIn("layout.plan", out)
        self.assertNotIn("Supply project.names_from", out)
        self.assertIn('names_from = "us"', (target / "config.toml").read_text())
        self.assertEqual(config.load_pending(target).state, "awaiting-roms")

    def test_text_failure_uses_stdout_and_one_next(self) -> None:
        with patch("unbake.cli.setup.run", side_effect=Held("setup", "layout.plan: missing")):
            code, out, error = self.run_main(self.args("setup"))
        self.assertEqual(code, 1)
        self.assertEqual(
            out,
            "HELD(setup): layout.plan: missing\nNext: Repair the prerequisite identified above. "
            f"Then run {command(self.root, 'setup')}.\n",
        )
        self.assertEqual(error, "")
