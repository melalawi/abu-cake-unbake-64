"""Real accepted source bytes reach maintenance without repeating their completed native proof."""

import hashlib
import json
import subprocess
from pathlib import Path
from unittest.mock import patch

from tests.kit import TESTS
from tests.project_fixture import ProjectCase
from unbake import cache, config, inputs, steps
from unbake.config import Held
from unbake.layout import header_step, merge_units, resident
from unbake.typemap import types_db

FIXTURE = (TESTS / "test_postcommit_key_work.py").parent / "fixtures/postcommit_key"
PROVENANCE = json.loads((FIXTURE / "provenance.json").read_text())


class PostcommitKeyWork(ProjectCase):
    versions = ("de", "eu", "eu-x", "us", "us-rev1")

    def git(self, *args):
        return subprocess.check_output(["git", *args], cwd=self.project.root, stderr=subprocess.PIPE).decode().strip()

    def accepted(self, function):
        payload = (FIXTURE / (function + ".c")).read_bytes()
        self.assertEqual(hashlib.sha256(payload).hexdigest(), PROVENANCE[function]["sha256"])
        self.assertEqual(list(self.versions), PROVENANCE[function]["accepted_versions"])
        source = self.project.src / (function + ".c")
        source.write_bytes(payload)
        # Only the existing published-unit identity changes in the small layout.
        for version in self.versions:
            for path in (self.project.version(version).split, self.project.version(version).symbols):
                path.write_text(
                    path.read_text().replace("alpha", function).replace(", asm, " + function, ", c, " + function)
                )
        layout = self.project.root / "layout.toml"
        layout.write_text(layout.read_text().replace("alpha", function))
        self.git("init", "-q")
        self.git("add", "config.toml", "layout.toml", "versions", "src", "include")
        self.git(
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.com",
            "commit",
            "-qm",
            "Accepted real source fixture",
        )
        self.project = config.load(self.project.root)
        cache.forget()
        return source, self.git("rev-parse", "HEAD")

    def maintenance(self, function):
        source, accepted = self.accepted(function)
        caught = None
        with (
            patch.object(merge_units, "run", wraps=merge_units.run) as maintained,
            patch.object(merge_units, "prove", side_effect=AssertionError("accepted proof repeated")) as proofs,
            patch.object(inputs, "file_pin", wraps=inputs.file_pin) as pins,
        ):
            try:
                results = steps.ensure(self.project, self.host, ["merge-units"])
            except Held as error:
                caught, results = error, []
            # Count first; old main stopped at a Path key part before entering the maintenance owner.
            self.assertEqual(maintained.call_count, 1)
            self.assertEqual(proofs.call_count, 0)
            self.assertIsNone(caught)
            self.assertEqual(sum(result.ran for result in results), 1)
            # Three key requests: initial, convergence, then the reused public request; seven named files each.
            repeated = steps.ensure(self.project, self.host, ["merge-units"])
            self.assertEqual(maintained.call_count, 1)
            self.assertEqual(sum(result.ran for result in repeated), 0)
            # Admission now also pins the command/step evidence. The owning
            # cache/read regression below verifies one physical read per input.
            pinned = {call.args[0] for call in pins.call_args_list}
            self.assertTrue({source, self.project.root / "layout.toml"} <= pinned)
        self.assertEqual(self.git("rev-parse", "HEAD"), accepted)
        self.assertEqual(self.git("rev-list", "--count", "HEAD"), "1")
        self.assertEqual(self.git("status", "--porcelain", "src", "layout.toml", "versions"), "")
        self.assertEqual(source.read_bytes(), (FIXTURE / (function + ".c")).read_bytes())
        self.assertEqual(len(self.project.versions), 5)

    def test_rw_b_matrix_postcommit_maintenance_keeps_the_accepted_prefix(self):
        self.maintenance("func_8029CE3C_de")

    def test_rw_clean_postcommit_maintenance_keeps_the_accepted_prefix(self):
        self.maintenance("func_80423828_de")

    def test_all_three_maintenance_owners_declare_files_and_reuse_digest_reads(self):
        source, _ = self.accepted("func_8029CE3C_de")
        paths = []
        original = Path.open

        def counted(path, *args, **kwargs):
            if (args and args[0] == "rb") or kwargs.get("mode") == "rb":
                paths.append(path)
            return original(path, *args, **kwargs)

        with patch.object(Path, "open", counted), patch.object(types_db, "solution", return_value=None):
            for owner in (merge_units, header_step, resident):
                first = owner.input_key(self.project)
                self.assertRegex(first, r"^[0-9a-f]{64}$")
                self.assertEqual(first, owner.input_key(self.project))
        self.assertEqual(paths.count(source), 1)
        self.assertEqual(len(paths), 11)  # 7 merge inputs + 3 owner recipes + the one installed header + no duplicates
        # Same bytes at a different logical source name still change the operation's identity.
        before = merge_units.input_key(self.project)
        renamed = source.with_name("renamed.c")
        source.rename(renamed)
        self.assertNotEqual(before, merge_units.input_key(self.project))
        changed = resident.input_key(self.project)
        renamed.write_bytes(renamed.read_bytes() + b"\n")
        self.assertNotEqual(changed, resident.input_key(self.project))
        with self.assertRaises(Held):
            cache.key(source)  # hard cutover remains strict
