"""Strict compiler-set project configuration and user policy validation."""

import hashlib
import json
import os
import runpy
import shutil
import tempfile
import tomllib
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from unbake.project import config

FIXTURE = Path(__file__).parents[1] / "fixture"


def write_policy(root: Path) -> Path:
    """Use the host tool facts with a measured, local test archive."""
    with Path(os.environ["UNBAKE_POLICY"]).open("rb") as source:
        values = tomllib.load(source)
    archive = root / "permuter.zip"
    with zipfile.ZipFile(archive, "w") as output:
        output.writestr("permuter/README", "Configuration test archive\n")
    values.update(
        cache_root=str(root / "cache"),
        state_root=str(root / "state"),
        permuter_archive=str(archive),
        permuter_sha256=hashlib.sha256(archive.read_bytes()).hexdigest(),
        search_beam=4,
    )
    path = root / "policy.toml"
    path.write_text("".join(f"{key} = {json.dumps(value)}\n" for key, value in values.items()))
    return path


class ConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.root = self.directory / "project"
        shutil.copytree(FIXTURE, self.root)
        self.path = self.root / "config.toml"
        self.original = self.path.read_text()

    def held_config(self, text: str, field: str) -> None:
        self.path.write_text(text)
        with self.assertRaises(config.Held) as raised:
            config.load(self.root)
        self.assertEqual(raised.exception.phase, "config")
        self.assertIn(field, raised.exception.reason)

    def test_loads_compiler_set_and_version_facts(self) -> None:
        project = config.load(self.root)
        self.assertEqual(project.versions, ("us", "us-rev1"))
        self.assertEqual(project.names_from, "us")
        compiler = project.compiler_for("src/alpha.c")
        self.assertEqual(compiler.id, "ido-7.1")
        self.assertEqual(compiler.cc, self.root / "tools/ido-7.1/cc")
        self.assertEqual(compiler.as_, self.root / "tools/ido-7.1/as1")
        self.assertEqual(compiler.cflags, ("-O2", "-G0", "-mips2"))
        self.assertEqual(project.version("us").macros, ("VERSION_US",))
        self.assertEqual(project.build_link("us"), self.root / "build/us")

    def test_single_version_and_explicit_empty_values(self) -> None:
        text = self.original.replace('versions = ["us", "us-rev1"]', 'versions = ["us"]')
        text = text.replace('names_from = "us"\n', "")
        text = text.replace('cflags = ["-O2", "-G0", "-mips2"]', "cflags = []")
        text = text.replace('macros = ["VERSION_US"]', "macros = []")
        self.path.write_text(text)
        project = config.load(self.root)
        self.assertEqual(project.versions, ("us",))
        self.assertEqual(project.names_from, "us")
        self.assertEqual(project.compiler_for("alpha").cflags, ())
        self.assertEqual(project.version("us").macros, ())

    def test_registry_flags_when_override_is_absent(self) -> None:
        self.path.write_text(self.original.replace('cflags = ["-O2", "-G0", "-mips2"]\n', ""))
        self.assertIn("-non_shared", config.load(self.root).compiler_for("alpha").cflags)

    def test_unit_path_stem_external_draft_and_conflict(self) -> None:
        extra = '[compilers."gcc-2.7.2-kmc"]\n'
        self.path.write_text(self.original.replace("[units]", extra + '[units]\nalpha = "gcc-2.7.2-kmc"'))
        project = config.load(self.root)
        self.assertEqual(project.compiler_for(self.directory / "drafts/alpha.c").id, "gcc-2.7.2-kmc")
        self.assertEqual(project.compiler_for("src/alpha.c").id, "gcc-2.7.2-kmc")
        self.path.write_text(self.path.read_text().replace("[units]", '[units]\n"src/alpha.c" = "ido-7.1"'))
        with self.assertRaisesRegex(config.Held, "conflicts"):
            config.load(self.root).compiler_for("src/alpha.c")

    def test_segment_compiler_selection_matches_standalone_recipe(self) -> None:
        from unbake.project import makefile

        self.path.write_text(
            self.original.replace("[units]", '[compilers."gcc-2.7.2-kmc"]\n[units]\nmain = "gcc-2.7.2-kmc"')
        )
        project = config.load(self.root)
        self.assertEqual(project.compiler_for("src/alpha.c").id, "gcc-2.7.2-kmc")
        self.assertEqual(makefile.description(project)["units"]["alpha"], "gcc-2.7.2-kmc")

    def test_missing_required_facts_are_named(self) -> None:
        fields = {
            "[project].name": 'name = "fixture"\n',
            "[project].title": 'title = "Synthetic three-function project"\n',
            "[project].names_from": 'names_from = "us"\n',
            "[project].versions": 'versions = ["us", "us-rev1"]\n',
            "[project].default_compiler": 'default_compiler = "ido-7.1"\n',
            "[paths].src": 'src = "src"\n',
            "[paths].include": 'include = ["include"]\n',
            "[paths].asm": 'asm = "asm"\n',
            "[paths].tools": 'tools = "tools"\n',
            "[version.us].baserom_sha1": 'baserom_sha1 = "21758de98b69907bcac48b82f833f14833a5f7cc"\n',
            "[version.us].split": 'split = "versions/us/fixture.yaml"\n',
            "[version.us].symbols": 'symbols = "versions/us/symbol_addrs.txt"\n',
            "[version.us].macros": 'macros = ["VERSION_US"]\n',
        }
        for field, line in fields.items():
            with self.subTest(field=field):
                self.held_config(self.original.replace(line, "", 1), field)
        for section in ("project", "paths", "units", "version.us"):
            with self.subTest(section=section):
                self.held_config(self.original.replace(f"[{section}]", "[unused]"), f"[{section}]")
        self.held_config(self.original.replace('[compilers."ido-7.1"]', "[unused_compiler]"), "[compilers]")

    def test_unknown_compiler_and_units_are_named(self) -> None:
        self.held_config(
            self.original.replace('default_compiler = "ido-7.1"', 'default_compiler = "unknown"'), "default_compiler"
        )
        self.held_config(self.original.replace("[units]", '[units]\nalpha = "unknown"'), "[units].alpha")
        self.held_config(self.original.replace('[compilers."ido-7.1"]', "[compilers.unknown]"), "[compilers.unknown]")

    def test_invalid_version_and_project_values(self) -> None:
        cases = (
            ('versions = ["us", "us-rev1"]', "versions = []", "versions"),
            ('versions = ["us", "us-rev1"]', 'versions = ["us", "us"]', "versions"),
            ('name = "fixture"', 'name = "../escape"', "name"),
            ('names_from = "us"', 'names_from = "eu"', "names_from"),
            ('names_from = "us"', 'names_from = ""', "names_from"),
            ('names_from = "us"', "names_from = false", "names_from"),
            ('macros = ["VERSION_US"]', "macros = false", "macros"),
        )
        for before, after, field in cases:
            with self.subTest(field=field):
                self.held_config(self.original.replace(before, after), field)

    def test_fixture_hashes_match_generator(self) -> None:
        runpy.run_path(str(self.root / "make_rom.py"))["write_roms"](self.root)
        project = config.load(self.root)
        for v in project.versions:
            self.assertEqual(
                hashlib.sha1(project.version(v).baserom.read_bytes()).hexdigest(), project.version(v).baserom_sha1
            )

    def test_user_policy_is_explicit_and_temp_scoped(self) -> None:
        path = write_policy(self.directory)
        policy = config.load_policy(path)
        self.assertEqual(policy.cache_root, self.directory / "cache")
        self.assertEqual(policy.state_root, self.directory / "state")
        self.assertEqual(policy.search_beam, 4)
        self.assertEqual(hashlib.sha256(policy.permuter_archive.read_bytes()).hexdigest(), policy.permuter_sha256)
        self.assertEqual(hashlib.sha256(policy.objdiff_cli.read_bytes()).hexdigest(), policy.objdiff_sha256)
        with (
            patch.dict("os.environ", {"XDG_CONFIG_HOME": str(self.directory / "missing"), "UNBAKE_POLICY": ""}),
            self.assertRaisesRegex(config.Held, "policy.toml"),
        ):
            config.load_policy()

    def test_missing_machine_policy_keys_are_named(self) -> None:
        path = write_policy(self.directory)
        original = path.read_text()
        defaults = self.directory / "defaults.toml"
        defaults.write_text("")
        for field in (
            "cache_root",
            "state_root",
            "objdiff_cli",
            "objdiff_sha256",
            "m2c",
            "splat",
            "mips_ld",
            "mips_objdump",
            "mips_readelf",
            "search_beam",
            "permuter_archive",
            "permuter_sha256",
        ):
            with self.subTest(field=field):
                path.write_text(
                    "\n".join(line for line in original.splitlines() if not line.startswith(field + " =")) + "\n"
                )
                with patch.object(config, "POLICY_PATH", defaults), self.assertRaisesRegex(config.Held, field):
                    config.load_policy(path)

    def test_invalid_policy_values_are_named(self) -> None:
        path = write_policy(self.directory)
        original = path.read_text()
        for field, invalid in (
            ("cores", "true"),
            ("search_beam", "true"),
            ("search_beam", "0"),
            ("permuter_archive", '"relative"'),
            ("permuter_sha256", '"bad"'),
            ("stall_trials", "0"),
            ("assignment_idle_hours", "nan"),
            ("cache_root", '"relative"'),
            ("objdiff_sha256", '"bad"'),
            ("init_same_game_similarity", "2"),
            ("init_split", '"unknown"'),
            ("init_probe_count", "0"),
        ):
            lines = [
                f"{field} = {invalid}" if line.startswith(field + " =") else line for line in original.splitlines()
            ]
            path.write_text("\n".join(lines) + "\n")
            with self.subTest(field=field), self.assertRaisesRegex(config.Held, field):
                config.load_policy(path)
