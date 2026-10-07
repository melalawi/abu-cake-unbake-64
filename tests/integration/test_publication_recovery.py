"""Real Git and recorded RW header slices exercise physical process-death recovery."""

import hashlib
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

from tests.kit import TESTS
from tests.project_fixture import ProjectCase
from unbake import atomic, buildfiles, journal, process
from unbake.config import Held
from unbake.inputs import DependencySet
from unbake.work import attempts

HEADER = (
    TESTS / "test_publication_recovery.py"
).parent / "fixtures/publication_headers/ragewars/after/include/common/types_1dc8418c21db.h"
CHILD = r"""
import os, sys
from pathlib import Path
from unbake import atomic, config, inputs, journal, process
from unbake.work import attempts
from tests.kit import host_values
from unbake.config import Host
root, stage = Path(sys.argv[1]), sys.argv[2]
project = config.load(root)
host = Host.from_values(host_values(root.parent / "child-host"), "draft")
with journal.transaction(project) as change:
    change.save([root / "include/owner.h", root / "include/new.h", root / "include/deleted.h"])
    if stage == "prepared": os._exit(97)
    atomic.write(root / "include/owner.h", b"struct Accepted {int value;};\n", mode=0o600)
    if stage == "first-install": os._exit(97)
    atomic.write(root / "include/new.h", b"typedef int New;\n")
    atomic.remove(root / "include/deleted.h")
    if stage == "installed": os._exit(97)
    def git(*args):
        return process.run_native(["git", *args], root, "fixture", temporary_root=project.build).stdout.strip()
    index = Path(git("rev-parse", "--path-format=absolute", "--git-path", "index"))
    paths = [root / "include/owner.h", root / "include/new.h", root / "include/deleted.h"]
    if stage in ("ledger-finalization", "torn-ledger"):
        source = root / "src/alpha.c"
        text = "int alpha(void) {return 1;}\n"
        atomic.text(source, text)
        pin = inputs.file_pin(source, root=root, root_id="project", reuse=False)
        dependencies = inputs.DependencySet((pin,), {}, {})
        attempts.ledger(project).record_publication("alpha", None, text, project.versions,
                                                  "ido-7.1", dependencies, committed=False)
        paths += [source, root / "attempts.jsonl"]
    trailer = change.prepare_commit(project, host, index, paths, git("rev-parse", "HEAD"))
    git("add", "--", *(str(p.relative_to(root)) for p in paths))
    if stage == "index": os._exit(97)
    git("commit", "-qm", "Accepted fixture\n\n" + trailer, "--only", "--", *(str(p.relative_to(root)) for p in paths))
    if stage == "git-commit": os._exit(97)
    if stage == "ledger-finalization":
        change.commit(git_commit=git("rev-parse", "HEAD"))
        os._exit(97)
    if stage == "torn-ledger":
        import unbake.work.attempts as a
        original = a.atomic.append_record
        def interrupted(path, content, *, durable):
            with path.open("ab") as stream:
                stream.write(content[:len(content)//2]); stream.flush(); os.fsync(stream.fileno())
            os._exit(97)
        a.atomic.append_record = interrupted
    journal.accepted(git_commit=git("rev-parse", "HEAD"))
    os._exit(97)
"""


class PublicationRecoveryTests(ProjectCase):
    def setUp(self):
        super().setUp()
        self.owner = self.project.include[0] / "owner.h"
        self.owner.write_bytes(HEADER.read_bytes())
        self.owner.chmod(0o640)
        self.old = self.owner.read_bytes()
        self.deleted = self.project.include[0] / "deleted.h"
        self.deleted.write_bytes(self.old)
        self.author = self.project.src / "untouched.c"
        self.author.write_text("/* pre-existing authored edit */\n")
        self.git("init", "-q")
        self.git("config", "user.name", "Fixture")
        self.git("config", "user.email", "fixture@example.test")
        self.git("add", ".")
        self.git("commit", "-qm", "Initial fixture")
        self.base = self.git("rev-parse", "HEAD").strip()
        (self.root / "child-host").mkdir()

    def git(self, *args):
        return subprocess.run(["git", *args], cwd=self.project.root, text=True, capture_output=True, check=True).stdout

    def crash(self, stage):
        result = subprocess.run(
            [sys.executable, "-c", CHILD, str(self.project.root), stage], text=True, capture_output=True
        )
        self.assertEqual((result.returncode, result.stderr), (97, ""))

    def test_death_before_git_restores_bytes_modes_absence_index_and_preserves_backups(self):
        for stage in ("prepared", "first-install", "installed", "index"):
            with self.subTest(stage=stage):
                index = Path(self.git("rev-parse", "--path-format=absolute", "--git-path", "index").strip())
                before = index.read_bytes()
                self.crash(stage)
                journal.recover_all(self.project)
                self.assertEqual(self.owner.read_bytes(), self.old)
                self.assertEqual(self.owner.stat().st_mode & 0o777, 0o640)
                self.assertTrue(self.deleted.exists())
                self.assertFalse((self.project.include[0] / "new.h").exists())
                self.assertEqual(index.read_bytes(), before)
                self.assertEqual(self.git("rev-parse", "HEAD").strip(), self.base)
                self.assertEqual(self.author.read_text(), "/* pre-existing authored edit */\n")
        archived = list((self.project.build / "publication.journal.archive").iterdir())
        self.assertEqual(len(archived), 4)
        self.assertTrue(all((p / journal.INDEX).is_file() for p in archived))

    def test_death_after_real_git_keeps_accepted_tree_and_makes_zero_extra_commits(self):
        for stage in ("git-commit", "committed"):
            with self.subTest(stage=stage):
                if stage == "committed":
                    # Restore only this owned tiny fixture through Git to its initial commit.
                    self.git("restore", "--source", self.base, "--", "include")
                    self.git("add", "include")
                    self.git("commit", "-qm", "Reset fixture inputs")
                self.crash(stage)
                accepted = self.git("rev-parse", "HEAD").strip()
                with patch.object(process, "run_native", wraps=process.run_native) as native:
                    journal.recover_all(self.project)
                self.assertEqual(native.call_count, 3)
                self.assertEqual(self.git("rev-parse", "HEAD").strip(), accepted)
                self.assertEqual(self.owner.read_bytes(), b"struct Accepted {int value;};\n")
                self.assertEqual(self.owner.stat().st_mode & 0o777, 0o600)
                self.assertFalse(self.deleted.exists())
                self.assertTrue((self.project.include[0] / "new.h").exists())
                journal.recover_all(self.project)
                self.assertEqual(self.git("rev-parse", "HEAD").strip(), accepted)

    def test_postcommit_ledger_finalization_and_torn_append_recover_exactly_one_outcome(self):
        for stage in ("ledger-finalization", "torn-ledger"):
            with self.subTest(stage=stage):
                if stage == "torn-ledger":
                    self.git("restore", "--source", self.base, "--", "include")
                    self.git("add", "include")
                    self.git("commit", "-qm", "Reset fixture inputs")
                self.crash(stage)
                head = self.git("rev-parse", "HEAD").strip()
                journal.recover_all(self.project)
                history = attempts.Ledger(self.project)
                history._refresh()
                committed = [r for r in history.events.values() if r["kind"] == "publication.exact"]
                self.assertEqual(len(committed), 1 if stage == "ledger-finalization" else 2)
                self.assertEqual(
                    history.publication("alpha").source_sha256,
                    hashlib.sha256((self.project.src / "alpha.c").read_bytes()).hexdigest(),
                )
                prefix = history.path.read_bytes()
                journal.recover_all(self.project)
                self.assertEqual(history.path.read_bytes(), prefix)
                self.assertEqual(self.git("rev-parse", "HEAD").strip(), head)

    def test_atomic_order_and_shared_hardlink_inode_are_preserved(self):
        other = self.project.include[0] / "old-inode.h"
        os.link(self.owner, other)
        seen = []
        fsync, replace = atomic.os.fsync, atomic.os.replace
        with (
            patch.object(atomic.os, "fsync", side_effect=lambda fd: (seen.append("sync"), fsync(fd))[1]),
            patch.object(atomic.os, "replace", side_effect=lambda a, b: (seen.append("replace"), replace(a, b))[1]),
        ):
            atomic.write(self.owner, b"replacement\n")
        self.assertEqual(seen, ["sync", "replace", "sync"])
        self.assertEqual(other.read_bytes(), self.old)
        self.assertNotEqual(other.stat().st_ino, self.owner.stat().st_ino)

    def test_corrupt_beforeimage_refuses_without_any_partial_restore(self):
        directory = self.project.build / "fixture.journal"
        changes = journal.Journal(directory, root=self.project.root).__enter__()
        atomic.write(self.owner, b"uncommitted\n")
        changes.recording.__exit__(None, None, None)
        journal._current.reset(changes.token)
        (directory / "0").write_bytes(b"corrupt")
        with self.assertRaises(Held):
            journal.recover(directory, root=self.project.root)
        self.assertEqual(self.owner.read_bytes(), b"uncommitted\n")
        self.assertTrue(directory.exists())

    def test_generated_ignore_preserves_lock_inode_and_tracks_authoritative_history(self):
        (self.project.root / ".gitignore").write_text("# authored rule\ncustom.tmp\n")
        with patch.object(buildfiles, "n64link_pin", return_value="fixture pin"):
            generated = buildfiles.generate(self.project, self.host)
        content = generated[self.project.root / ".gitignore"]
        self.assertEqual(content, b"# authored rule\ncustom.tmp\n/.attempts.lock\n")
        atomic.write(self.project.root / ".gitignore", content)
        attempts.Ledger(self.project).note("history.fixture", "runtime", {}, dependencies=DependencySet((), {}, {}))
        lock = self.project.root / ".attempts.lock"
        inode = lock.stat().st_ino
        attempts.Ledger(self.project).summaries()
        self.assertEqual(lock.stat().st_ino, inode)
        self.assertEqual(self.git("check-ignore", ".attempts.lock").strip(), ".attempts.lock")
        self.git("add", ".gitignore", attempts.PATH)
        self.git("commit", "-qm", "Generated runtime hygiene")
        self.assertEqual(self.git("status", "--porcelain", "--untracked-files=all"), "")
        self.assertEqual(self.git("ls-files", "--", attempts.PATH).strip(), attempts.PATH)
