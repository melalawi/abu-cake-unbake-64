"""BT6 actual staged B1520 config must coexist with a scoped 14970 commit."""

import hashlib
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import toml

from tests.project_fixture import ProjectCase
from unbake import journal, land, process
from unbake.compilers.recipe_options import UnitRecipe
from unbake.config import Held

FIXTURE = Path(__file__).parent / "fixtures/data_emission"
FUNCTION = "func_80114970_us"
OTHER = "func_800B1520_us"


class LandConfigScopeTests(ProjectCase):
    def setUp(self):
        super().setUp()
        self.path = self.project.root / "config.toml"
        self.head = (FIXTURE / "bt-head.toml").read_bytes()
        self.staged = (FIXTURE / "bt-staged.toml").read_bytes()
        self.local = self.staged
        self.path.write_bytes(self.local)
        values = toml.loads(self.head.decode())
        default = values["project"]["default_compiler"]
        self.project = replace(
            self.project, default_compiler=default, units={self.project.unit_path(FUNCTION): UnitRecipe(default)}
        )
        self.index = self.project.root / ".git/index"
        self.index.parent.mkdir()
        self.index.write_bytes(b"preserved unrelated index entries")
        self.calls = []
        self.blobs = {}
        self.revision = "5a01eaed3ec9bfa32eaea921186c823253e74347"

    def native(self, argv, cwd, phase, **kwargs):
        self.calls.append(tuple(argv))
        args = argv[1:]
        if args[:1] == ["status"]:
            stdout = "M  config.toml\0"
        elif args == ["show", "HEAD:config.toml"]:
            stdout = self.head.decode()
        elif args == ["show", ":config.toml"]:
            stdout = self.staged.decode()
        elif args == ["rev-parse", "--git-path", "index"]:
            stdout = str(self.index)
        elif args == ["rev-parse", "HEAD"]:
            stdout = self.revision
        elif args[:1] == ["ls-tree"]:
            stdout = "config.toml\0"
        elif args[:1] == ["add"]:
            self.staged = self.path.read_bytes()
            stdout = ""
        elif "commit" in args:
            self.assertIn("--only", args)
            self.assertEqual(args[args.index("--") + 1 :], ["config.toml"])
            self.head = self.path.read_bytes()
            self.revision = "a" * 40
            stdout = ""
        elif args[:1] == ["hash-object"]:
            image = Path(args[-1]).read_bytes()
            stdout = hashlib.sha1(b"blob " + str(len(image)).encode() + b"\0" + image).hexdigest()
            self.blobs[stdout] = image
        elif args[:1] == ["update-index"]:
            self.assertEqual(args[1:3], ["--cacheinfo", "100644"])
            self.assertEqual(args[-1], "config.toml")
            self.staged = self.blobs[args[3]]
            stdout = ""
        else:
            self.fail(f"unexpected native boundary: {argv}")
        return process.NativeResult(
            tuple(argv), str(cwd), 0, None, stdout, "", "success", None, "utf-8", "surrogateescape", {}
        )

    def test_actual_bt_target_commit_excludes_staged_b1520_and_restores_local_index(self):
        initial = toml.loads(self.head.decode())
        staged = toml.loads(self.staged.decode())
        self.assertEqual(staged["units"][OTHER], {"compiler": "gcc-2.7.2-kmc", "flags": ["-UF3DEX_GBI_2"]})
        self.assertNotEqual(initial["units"].get(OTHER), staged["units"][OTHER])
        prior_index = self.index.read_bytes()
        with patch.object(process, "run_native", side_effect=self.native), journal.transaction(self.project):
            committed = land._compiler_config(self.project, FUNCTION, "ido-7.1", self.local, staged_before=self.staged)
            local = land._unit_config(self.project, FUNCTION, "ido-7.1", self.local)
            index = land._unit_config(self.project, FUNCTION, "ido-7.1", self.staged)
            self.path.write_bytes(committed)
            land._commit(self.project, self.host, [self.path], "Match " + FUNCTION)
            land._restore_compiler_config(self.project, self.host, local, index)
        unit = self.project.unit_path(FUNCTION)
        recipe = UnitRecipe("ido-7.1").document()
        expected = {**initial, "units": {**initial["units"], unit: recipe}}
        self.assertEqual(toml.loads(self.head.decode()), expected)
        self.assertEqual(toml.loads(self.path.read_text())["units"][OTHER], staged["units"][OTHER])
        self.assertEqual(toml.loads(self.staged.decode())["units"][OTHER], staged["units"][OTHER])
        self.assertEqual(toml.loads(self.staged.decode())["units"][unit], recipe)
        self.assertEqual(self.index.read_bytes(), prior_index)

    def test_unstaged_other_unit_and_staged_or_local_global_changes_still_refuse(self):
        for change in ("unstaged-unit", "global-local", "global-staged"):
            with self.subTest(change=change):
                self.head = (FIXTURE / "bt-head.toml").read_bytes()
                self.staged = (FIXTURE / "bt-staged.toml").read_bytes()
                local = toml.loads(self.staged.decode())
                if change == "unstaged-unit":
                    local["units"][OTHER]["flags"] = ["-O1"]
                elif change == "global-local":
                    local["build"]["cppflags"].append("-DUNPROVED")
                else:
                    staged = toml.loads(self.staged.decode())
                    staged["build"]["cppflags"].append("-DUNPROVED")
                    self.staged = toml.dumps(staged).encode()
                before = toml.dumps(local).encode()
                self.path.write_bytes(before)
                prior_index = self.index.read_bytes()
                with (
                    patch.object(process, "run_native", side_effect=self.native),
                    self.assertRaisesRegex(Held, "land.config"),
                ):
                    land._compiler_config(self.project, FUNCTION, "ido-7.1", before)
                self.assertEqual(self.path.read_bytes(), before)
                self.assertEqual(self.index.read_bytes(), prior_index)

    def test_config_worktree_and_index_changes_during_proof_refuse_without_writes(self):
        before = self.local
        with patch.object(process, "run_native", side_effect=self.native):
            self.path.write_bytes(before + b"\n")
            with self.assertRaisesRegex(Held, "changed since proof"):
                land._compiler_config(self.project, FUNCTION, "ido-7.1", before, staged_before=self.staged)
            self.path.write_bytes(before)
            staged_before = self.staged
            self.staged += b"\n"
            with self.assertRaisesRegex(Held, "index changed since proof"):
                land._compiler_config(self.project, FUNCTION, "ido-7.1", before, staged_before=staged_before)
        self.assertEqual(self.path.read_bytes(), before)


class LandMigrationConfigTests(ProjectCase):
    """The tool's own migrated config.toml is admitted with a separate migration commit; hand edits are not."""

    native = LandConfigScopeTests.native

    def setUp(self):
        super().setUp()
        import json

        self.path = self.project.root / "config.toml"
        self.head = (FIXTURE / "bt-head.toml").read_bytes()
        self.index = self.project.root / ".git/index"
        self.index.parent.mkdir()
        self.index.write_bytes(b"x")
        self.calls, self.blobs = [], {}
        self.revision = "5a01eaed3ec9bfa32eaea921186c823253e74347"

        migrated = toml.loads(self.head.decode())
        migrated["schema"] = 2
        self.migrated = toml.dumps(migrated).encode()
        self.staged = self.head
        self.path.write_bytes(self.migrated)
        directory = self.project.root / ".unbake/migrations/abc"
        directory.mkdir(parents=True)
        (directory / "config.toml").write_bytes(self.head)
        (directory / "plan.json").write_text(
            json.dumps(
                {"kind": "config.recipe-cutover", "writes": {"config.toml": self.migrated.decode()}, "file_copies": {}}
            )
        )

    def test_migration_output_is_recognised_and_committed_alone(self):
        with patch.object(process, "run_native", side_effect=self.native), journal.transaction(self.project):
            self.assertIsNotNone(land.migrated_config(self.project))
            commit = land.commit_migration(self.project, self.host)
        self.assertEqual(commit, "a" * 40)
        self.assertEqual(self.head, self.migrated)

    def test_hand_edit_on_top_of_migration_is_not_admitted_and_refusal_names_keys(self):
        edited = toml.loads(self.migrated.decode())
        edited["build"]["cppflags"].append("-DUNPROVED")
        self.path.write_bytes(toml.dumps(edited).encode())
        with patch.object(process, "run_native", side_effect=self.native):
            self.assertIsNone(land.migrated_config(self.project))
            with self.assertRaisesRegex(Held, r"differing keys: .*build\.cppflags"):
                land._compiler_config(self.project, FUNCTION, "ido-7.1", self.path.read_bytes())
