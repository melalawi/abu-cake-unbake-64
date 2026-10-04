"""Landing one function: write on pass, nothing on mismatch, one retry when the tree changed."""

import tomllib
from types import SimpleNamespace
from unittest.mock import patch

from tests.kit import TempCase, host_values
from unbake import land
from unbake.config import Host

# DRAFT interface: the boundary names below are not in INTERFACES section 13; adjust at merge time.
BUILD = "build_candidate"  # (project, host, function, source) -> {version: Candidate(ok, rom_new)}
TREE = "tree_state"  # (project) -> token that changes when another land happened
GIT = "commit"  # (project, host, function, paths) -> sha


class LandTests(TempCase):
    def setUp(self) -> None:
        super().setUp()
        for name in ("src", "build/us", "build/us-rev1"):
            (self.root / name).mkdir(parents=True)
        (self.root / "layout.toml").write_text('schema = 1\n[[group]]\nname = "g"\nmembers = ["alpha"]\n')
        self.source = self.root / "build" / "work" / "alpha" / "alpha.c"
        self.source.parent.mkdir(parents=True)
        self.source.write_text("int alpha(void) { return 1; }\n")
        self.layout_before = (self.root / "layout.toml").read_bytes()
        self.project = SimpleNamespace(
            root=self.root, build=self.root / "build", src=self.root / "src", versions=("us", "us-rev1")
        )
        self.host = Host.from_values(host_values(self.root), "publish")
        self.commits: list[str] = []
        self.tree = ["t0"]
        self.results: list[dict[str, bool]] = []

    def candidate(self, project, host, function, source):
        verdicts = self.results.pop(0)
        made = {}
        for version, ok in verdicts.items():
            rom = self.root / "build" / version / "fixture.z64.new"
            rom.write_bytes(b"new")
            made[version] = SimpleNamespace(ok=ok, rom_new=rom)
        return made

    def run_land(self):
        def commit(project, host, function, paths):
            self.commits.append(function)
            return "c0ffee"

        with (
            patch.object(land, BUILD, side_effect=self.candidate),
            patch.object(land, TREE, side_effect=lambda project: self.tree[-1]),
            patch.object(land, GIT, side_effect=commit),
        ):
            return land.land(self.project, self.host, "alpha", self.source)

    def assert_nothing_written(self) -> None:
        self.assertFalse((self.root / "src" / "alpha.c").exists())
        self.assertEqual((self.root / "layout.toml").read_bytes(), self.layout_before)
        self.assertEqual(self.commits, [])
        self.assertEqual(list(self.root.glob("build/*/*.new")), [])

    def test_pass_writes_source_and_member_and_commits_once(self) -> None:
        self.results = [{"us": True, "us-rev1": True}]
        result = self.run_land()
        self.assertIsInstance(result, land.Landed)
        self.assertEqual((result.function, result.commit, result.retried), ("alpha", "c0ffee", False))
        self.assertEqual(set(result.versions), {"us", "us-rev1"})
        self.assertEqual((self.root / "src" / "alpha.c").read_text(), self.source.read_text())
        self.assertEqual(self.commits, ["alpha"])
        self.assertNotEqual((self.root / "layout.toml").read_bytes(), self.layout_before)
        tomllib.loads((self.root / "layout.toml").read_text())

    def test_mismatch_in_any_version_writes_nothing_and_removes_the_new_rom(self) -> None:
        for label, verdicts in [("second version", {"us": True, "us-rev1": False}), ("first", {"us": False, "us-rev1": True})]:
            with self.subTest(label):
                self.results = [verdicts]
                result = self.run_land()
                self.assertIsInstance(result, land.LandFailed)
                self.assertEqual(result.function, "alpha")
                self.assertTrue(result.diagnostic)
                self.assert_nothing_written()

    def test_changed_tree_retries_once_and_can_then_land(self) -> None:
        self.results = [{"us": False, "us-rev1": True}, {"us": True, "us-rev1": True}]
        original = self.candidate

        def moved(*args):
            self.tree.append("t2")
            return original(*args)

        self.candidate = moved  # type: ignore[method-assign]
        result = self.run_land()
        self.assertIsInstance(result, land.Landed)
        self.assertTrue(result.retried)

    def test_unchanged_tree_does_not_retry(self) -> None:
        self.results = [{"us": False, "us-rev1": True}, {"us": True, "us-rev1": True}]
        result = self.run_land()
        self.assertIsInstance(result, land.LandFailed)
        self.assertEqual(len(self.results), 1)
        self.assert_nothing_written()

    def test_second_failure_after_retry_is_returned_to_the_worker(self) -> None:
        self.results = [{"us": False}, {"us": False}, {"us": True}]
        original = self.candidate

        def moved(*args):
            self.tree.append(f"t{len(self.tree)}")
            return original(*args)

        self.candidate = moved  # type: ignore[method-assign]
        result = self.run_land()
        self.assertIsInstance(result, land.LandFailed)
        self.assertTrue(result.returned_to_worker)
        self.assertEqual(len(self.results), 1)
        self.assert_nothing_written()

