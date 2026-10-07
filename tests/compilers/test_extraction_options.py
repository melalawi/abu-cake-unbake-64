"""Effective extraction and native options using real RW and BT config slices."""

import re
import tomllib
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from tests.project_fixture import ProjectCase
from unbake import config, extract
from unbake.compilers import drivers, registry

FIXTURES = Path(__file__).parent / "fixtures" / "config"


class ExtractionOptionsTests(ProjectCase):
    versions = ("us",)

    def game(self, game):
        slices = (FIXTURES / (game + ".toml")).read_text()
        base = (self.project.root / "config.toml").read_text()
        default = tomllib.loads(slices)["project"]["default_compiler"]
        base = re.sub(r"^default_compiler = .*$", f'default_compiler = "{default}"', base, flags=re.M)
        base = re.sub(r"^\[compilers\.[^\n]+\]\n.*?(?=^\[|\Z)", "", base, flags=re.M | re.S)
        base = re.sub(r"^\[build\]\n.*?(?=^\[|\Z)", "", base, flags=re.M | re.S)
        base += slices[slices.index("[build]") :]
        (self.project.root / "config.toml").write_text(base)
        return config.load(self.project.root)

    def test_real_bt_and_rw_extraction_modes(self):
        for game, mode in (("battletanx", "KMC"), ("ragewars", "SN64")):
            with self.subTest(game=game):
                project = self.game(game)
                options = extract._options(project, "us", self.root / "stage", self.root / "symbols")
                self.assertEqual(options["compiler"], mode)
                self.assertEqual(len(options), 17)

    def test_native_paths_flags_and_legacy_config_remain_effective(self):
        for game in ("battletanx", "ragewars"):
            project = self.game(game)
            before = (project.root / "config.toml").read_bytes()
            self.assertEqual(project.gnu_asflags, ("-mips3",))
            self.assertEqual(config.load_pending(project.root).gnu_asflags, ("-mips3",))
            for ident in project.compilers:
                with self.subTest(game=game, compiler=ident):
                    selected = replace(project, default_compiler=ident)
                    steps = drivers.steps(selected, "us", "alpha", "src/alpha.c", drivers.Tools("cpp", "as", "n64link"))
                    spec = registry.specification(ident)
                    self.assertEqual(steps.kind, spec.kind)
                    self.assertEqual(steps.assemble is not None, spec.kind == "gnu")
                    self.assertEqual(steps.preprocess[0], "cpp" if spec.kind == "gnu" else f"tools/{ident}/cc")
                    self.assertEqual(
                        sum(x is not None for x in (steps.preprocess, steps.compile, steps.assemble)),
                        3 if spec.kind == "gnu" else 2,
                    )
                    for flag in drivers.codegen_flags(list(selected.compilers[ident].cflags)):
                        self.assertIn(flag, steps.compile)
                    self.assertEqual(extract._options(selected, "us", self.root, self.root)["compiler"], spec.splat)
            self.assertEqual((project.root / "config.toml").read_bytes(), before)
            canonical = before.decode().replace("sn64_asflags", "gnu_asflags")
            self.assertEqual(config.load(project.root, text=canonical).gnu_asflags, project.gnu_asflags)

    def test_extraction_key_tracks_mode_and_default_compiler(self):
        project = self.game("battletanx")
        spec = registry.specification(project.default_compiler)
        with (
            patch("unbake.inputs.digest", return_value="splat"),
            patch.object(extract, "splat_rows", return_value="rows"),
        ):
            original = extract._version_key(project, self.host, "us")
            with patch.object(registry, "specification", return_value=replace(spec, splat="SN64")):
                self.assertNotEqual(extract._version_key(project, self.host, "us"), original)

    def test_conflicting_old_and_new_keys_are_refused(self):
        project = self.game("ragewars")
        text = (project.root / "config.toml").read_text().replace("[build]", '[build]\ngnu_asflags = ["-different"]')
        with self.assertRaisesRegex(config.Held, "conflicts with sn64_asflags"):
            config.load(project.root, text=text)
