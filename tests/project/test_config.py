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
        same_game_similarity=0.1,
        symbol_similarity_threshold=0.9,
        symbol_similarity_margin=0.1,
        probe_count=20,
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
        self.directory = Path(self.temporary.name).resolve()
        self.root = self.directory / "project"
        shutil.copytree(FIXTURE, self.root)
        self.path = self.root / "config.toml"
        self.original = self.path.read_text()

    def test_checkout_identity_is_local_and_ignores_retired_config(self) -> None:
        first = config.load(self.root)
        self.assertEqual(config.load(self.root).checkout_id, first.checkout_id)
        self.assertEqual(self.path.read_text(), self.original)
        self.path.write_text(
            self.original.replace('id = "00000000-0000-4000-8000-000000000002"', 'id = "invalid-retired-id"')
        )
        self.assertEqual(config.load(self.root).checkout_id, first.checkout_id)
        self.assertFalse((self.root / ".unbake/workspace-id").exists())
        other = self.directory / "other"
        shutil.copytree(FIXTURE, other)
        self.assertNotEqual(config.load(other).checkout_id, first.checkout_id)

    def test_clone_source_load_does_not_create_checkout_state(self) -> None:
        before = self.path.read_bytes()
        config.load(self.root)
        self.assertFalse((self.root / ".unbake").exists())
        self.assertEqual(self.path.read_bytes(), before)

    def test_refresh_strips_workspace_once(self) -> None:
        from unbake.project.setup_config import strip_workspace

        stripped = strip_workspace(self.original)
        self.assertNotIn("workspace", tomllib.loads(stripped))
        self.assertEqual(strip_workspace(stripped), stripped)

    def held_config(self, text: str, field: str) -> None:
        self.path.write_text(text)
        with self.assertRaises(config.Held) as raised:
            config.load(self.root)
        self.assertEqual(raised.exception.phase, "config")
        self.assertIn(
            field.replace("[", "").replace("]", ""), raised.exception.reason.replace("[", "").replace("]", "")
        )

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

    def test_declaration_evidence_is_an_explicit_external_or_relative_include_tree(self) -> None:
        for values, expected in (
            ([], ()),
            (["old/include"], (self.root / "old/include",)),
            (["/external/include"], (Path("/external/include"),)),
        ):
            with self.subTest(values=values):
                self.path.write_text(
                    self.original.replace("[paths]", "[paths]\ndeclaration_evidence = " + json.dumps(values))
                )
                self.assertEqual(config.load(self.root).declaration_evidence, expected)
        self.held_config(
            self.original.replace("[paths]", '[paths]\ndeclaration_evidence = "bad"'), "paths.declaration_evidence"
        )

    def test_single_version_and_explicit_empty_values(self) -> None:
        text = self.original.replace('versions = ["us", "us-rev1"]', 'versions = ["us"]')
        text = text.replace('cflags = ["-O2", "-G0", "-mips2"]', "cflags = []")
        text = text.replace('macros = ["VERSION_US"]', "macros = []")
        self.path.write_text(text)
        project = config.load(self.root)
        self.assertEqual(project.versions, ("us",))
        self.assertEqual(project.names_from, "us")
        self.assertEqual(project.compiler_for("alpha").cflags, ())
        self.assertEqual(project.version("us").macros, ())

    def test_confirmed_flags_are_required(self) -> None:
        self.held_config(self.original.replace('cflags = ["-O2", "-G0", "-mips2"]\n', ""), "cflags")

    def test_unit_path_stem_external_draft_and_conflict(self) -> None:
        extra = '[compilers."gcc-2.7.2-kmc"]\ncflags = []\n'
        self.path.write_text(self.original.replace("[units]", extra + '[units]\nalpha = "gcc-2.7.2-kmc"'))
        project = config.load(self.root)
        self.assertEqual(project.compiler_for(self.directory / "drafts/alpha.c").id, "gcc-2.7.2-kmc")
        self.assertEqual(project.compiler_for("src/alpha.c").id, "gcc-2.7.2-kmc")
        self.path.write_text(self.path.read_text().replace("[units]", '[units]\n"src/alpha.c" = "ido-7.1"'))
        # [units] keys are function names; a path is refused by name.
        with self.assertRaisesRegex(config.Held, r"\[units\]\.src/alpha\.c: expected a function name"):
            config.load(self.root).compiler_for("src/alpha.c")

    def test_unit_compiler_selection_matches_standalone_recipe(self) -> None:
        from unbake.project import makefile

        # Exception units are listed by function name; a segment selects nothing.
        self.path.write_text(
            self.original.replace(
                "[units]", '[compilers."gcc-2.7.2-kmc"]\ncflags = []\n[units]\nalpha = "gcc-2.7.2-kmc"'
            )
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
        for section in ("project", "paths"):
            with self.subTest(section=section):
                self.held_config(self.original.replace(f"[{section}]", "[unused]"), f"[{section}]")
        # A listed version needs its own table.
        self.held_config(self.original.replace("[version.us]", '[version."us-other"]'), "[version.us]")
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
            self.assertRaisesRegex(config.Held, "policy.cache_root"),
        ):
            config.load_policy()

    def test_missing_cache_root_names_policy_and_valid_example(self) -> None:
        path = write_policy(self.directory)
        path.write_text(
            "\n".join(line for line in path.read_text().splitlines() if not line.startswith("cache_root ="))
        )
        with self.assertRaises(config.Held) as refused:
            config.load_policy(path)
        self.assertIn(str(path), refused.exception.reason)
        example = refused.exception.reason.split("; add ", 1)[1]
        self.assertTrue(Path(tomllib.loads(example)["cache_root"]).is_absolute())
        path.write_text(path.read_text() + "\n" + example + "\n")
        self.assertEqual(config.load_policy(path).cache_root, path.parent / "cache")

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
            "setup_version_jobs",
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
            ("same_game_similarity", "2"),
            ("probe_count", "0"),
        ):
            lines = [
                f"{field} = {invalid}" if line.startswith(field + " =") else line for line in original.splitlines()
            ]
            path.write_text("\n".join(lines) + "\n")
            with self.subTest(field=field), self.assertRaisesRegex(config.Held, field):
                config.load_policy(path)

    def test_schema_identity_and_structural_fields_are_required(self) -> None:
        for line, field in (
            ("schema = 1\n", "schema"),
            ('id = "00000000-0000-4000-8000-000000000001"\n', "project.id"),
            ('state = "ready"\n', "project.state"),
            ('roms = "roms"\n', "paths.roms"),
            ('build = "build"\n', "paths.build"),
            ('work = "build/work"\n', "paths.work"),
            ('drafts = "build/drafts"\n', "paths.drafts"),
            ('baserom = "roms/baserom.us.z64"\n', "baserom"),
        ):
            with self.subTest(field=field):
                self.assertIn(line, self.original)
                self.held_config(self.original.replace(line, "", 1), field)
        for before, after, field in (
            ("schema = 1", "schema = true", "schema"),
            ('state = "ready"', 'state = "legacy"', "project.state"),
            ('work = "build/work"', 'work = "src/work"', "paths.work"),
            ('drafts = "build/drafts"', 'drafts = "build/work/drafts"', "paths.work"),
            ('work = "build/work"', 'work = "build/us"', "paths.work/paths.drafts"),
            ('build = "build"', 'build = ".git"', "paths.build"),
            ('build = "build"', 'build = "src/output"', "paths.build"),
            ('build = "build"', 'build = "../outside"', "paths.build"),
            ('baserom = "roms/baserom.us.z64"', 'baserom = "baserom.us.z64"', "version.us.baserom"),
        ):
            with self.subTest(field=field, after=after):
                self.held_config(self.original.replace(before, after, 1), field)

    def test_structural_symlink_escape_and_loop_are_named(self) -> None:
        link = self.root / "escaped"
        link.symlink_to(self.directory, target_is_directory=True)
        loop = self.root / "loop"
        loop.symlink_to(loop)
        for destination in ("escaped/work", "loop"):
            with self.subTest(destination=destination):
                self.held_config(self.original.replace('work = "build/work"', f'work = "{destination}"'), "paths.work")

    def test_setup_policy_does_not_require_search_tools(self) -> None:
        path = write_policy(self.directory)
        original = path.read_text()
        for field in ("m2c", "objdiff_cli", "objdiff_sha256", "permuter_archive", "permuter_sha256"):
            original = "\n".join(line for line in original.splitlines() if not line.startswith(field + " =")) + "\n"
        path.write_text(original)
        self.assertIsInstance(config.load_policy(path, stage="setup"), config.SetupPolicy)
        self.assertIsInstance(config.load_policy(path, stage="census"), config.CensusPolicy)
        for field in (
            "splat",
            "mips_as",
            "mips_ld",
            "mips_objcopy",
            "cpp",
            "cache_root",
            "asflags",
            "cppflags",
            "sn64_asflags",
        ):
            path.write_text(
                "\n".join(line for line in original.splitlines() if not line.startswith(field + " =")) + "\n"
            )
            with self.subTest(field=field), self.assertRaisesRegex(config.Held, "policy." + field):
                config.load_policy(path, stage="setup")
        path.write_text(original.replace("same_game_similarity = 0.1", "same_game_similarity = nan"))
        with self.assertRaisesRegex(config.Held, "policy.same_game_similarity"):
            config.load_policy(path, stage="census")

    def test_setup_version_jobs_is_required_and_has_no_packaged_default(self) -> None:
        path = write_policy(self.directory)
        original = path.read_text()
        path.write_text("\n".join(row for row in original.splitlines() if not row.startswith("setup_version_jobs")))
        self.assertIsInstance(config.load_policy(path, stage="census"), config.CensusPolicy)
        for stage in ("setup", "all"):
            with self.subTest(stage=stage), self.assertRaisesRegex(config.Held, "policy.setup_version_jobs"):
                config.load_policy(path, stage=stage)
        for value in (0, -1, True, 1.5):
            path.write_text(original + "\n")
            rows = [row for row in original.splitlines() if not row.startswith("setup_version_jobs")]
            path.write_text("\n".join(rows) + "\nsetup_version_jobs = " + json.dumps(value) + "\n")
            with self.subTest(value=value), self.assertRaisesRegex(config.Held, "policy.setup_version_jobs"):
                config.load_policy(path, stage="setup")

    def test_missing_policy_creates_only_an_incomplete_template(self) -> None:
        path = self.directory / "operator/policy.toml"
        with self.assertRaisesRegex(config.Held, "policy.cache_root"):
            config.load_policy(path, stage="setup")
        text = path.read_text()
        values = tomllib.loads(text)
        self.assertNotIn("splat", values)
        self.assertNotIn("cache_root", values)
        self.assertIn("# splat = <required value>", text)
        self.assertNotIn("init_split", text)

    def test_guidance_policy_read_does_not_create_a_template(self) -> None:
        path = self.directory / "absent-operator/policy.toml"
        with self.assertRaisesRegex(config.Held, "policy.path"):
            config.read_policy(path)
        self.assertFalse(path.parent.exists())
