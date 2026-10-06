"""Landing one function: write and commit only after every holding version proves; restore on any failure."""

import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from tests.project_fixture import ProjectCase
from unbake import config, land
from unbake.config import Held
from unbake.fold.apply import Folded

SOURCE = "int alpha(void) { return 1; }\n"


class LandTests(ProjectCase):
    def setUp(self) -> None:
        super().setUp()
        self.file = self.project.work / "alpha" / "alpha.c"
        self.file.parent.mkdir(parents=True)
        self.file.write_text(SOURCE)
        self.splits = {v: self.project.version(v).split.read_text() for v in self.project.versions}
        self.git: list[tuple[str, ...]] = []
        self.fail_commit = False

    def run_land(self, proved: object, headers: dict[str, str] | None = None) -> str:
        def git(project, *args, env=None):
            self.git.append(args)
            if args[-1:] == ("HEAD",):
                return "c0ffee\n"
            if "commit" in args and self.fail_commit:
                raise Held("land", "git commit exited 1: hook refused")
            return ""

        prove = {"side_effect": proved} if isinstance(proved, Exception) else {"return_value": proved}
        with (
            patch.object(land, "exact_attempt", return_value=SimpleNamespace(compiler="ido-7.1")),
            patch("unbake.fold.apply.fold", return_value=Folded("alpha", SOURCE, headers or {}, ())),
            patch("unbake.fold.apply.private_headers", return_value={}),
            patch.object(land, "prove", **prove),
            patch.object(land, "_git", side_effect=git),
            patch.object(land.buildfiles, "write", return_value=[]),
            patch.object(land.steps, "record"),
            patch("unbake.report.progress.write", return_value=[]),
        ):
            return land.land(self.project, self.host, self.file)

    def assert_untouched(self) -> None:
        self.assertFalse((self.project.src / "alpha.c").exists())
        for version, text in self.splits.items():
            self.assertEqual(self.project.version(version).split.read_text(), text)

    def test_pass_writes_source_and_rows_and_commits_once(self) -> None:
        self.assertEqual(self.run_land(["us", "eu"]), "c0ffee")
        self.assertEqual((self.project.src / "alpha.c").read_text(), SOURCE)
        for version in self.project.versions:
            self.assertIn("c, alpha]", self.project.version(version).split.read_text())
        commits = [args for args in self.git if "commit" in args]
        self.assertEqual(len(commits), 1)
        self.assertIn("Match alpha", commits[0])
        added = next(args for args in self.git if args[0] == "add")
        self.assertIn("src/alpha.c", added)
        self.assertFalse(self.file.parent.exists())

    def test_mismatch_writes_nothing(self) -> None:
        with self.assertRaisesRegex(Held, "land.mismatch"):
            self.run_land(Held("land", "land.mismatch: alpha eu: linked bytes differ from the ROM row"))
        self.assert_untouched()
        self.assertEqual(self.git, [])
        self.assertTrue(self.file.is_file())

    def test_commit_failure_restores_every_written_file(self) -> None:
        self.fail_commit = True
        with self.assertRaisesRegex(Held, "hook refused"):
            self.run_land(["us", "eu"])
        self.assert_untouched()

    def test_published_unit_lands_again_as_clean_with_no_row_edits(self) -> None:
        (self.project.src / "alpha.c").write_text("int alpha(void) { do {} while (0); return 1; }\n")
        for version in self.project.versions:
            split = self.project.version(version).split
            split.write_text(split.read_text().replace("asm, alpha]", "c, alpha]"))
        self.project = config.load(self.project.root)
        published = {v: self.project.version(v).split.read_text() for v in self.project.versions}
        self.assertEqual(self.run_land(["us", "eu"]), "c0ffee")
        self.assertEqual((self.project.src / "alpha.c").read_text(), SOURCE)
        self.assertEqual({v: self.project.version(v).split.read_text() for v in self.project.versions}, published)
        commits = [args for args in self.git if "commit" in args]
        self.assertEqual(len(commits), 1)
        self.assertIn("Clean alpha", commits[0])

    def test_landing_with_default_compiler_keeps_proven_unit_flags(self) -> None:
        path = self.project.root / "config.toml"
        path.write_text(
            path.read_text().replace("[units]", '[units]\nalpha = { compiler = "ido-7.1", flags = ["-O1"] }')
        )
        self.project = config.load(self.project.root)
        self.assertEqual(self.run_land(["us", "eu"]), "c0ffee")
        landed = config.load(self.project.root)
        self.assertEqual(landed.compiler_reference("alpha"), "ido-7.1")
        self.assertEqual(landed.unit_flags["alpha"], ("-O1",))

    def test_successful_land_acknowledges_only_its_proven_header_output(self) -> None:
        from unbake import inputs, steps

        header = self.project.include[-1] / "main/alpha.h"
        other = self.project.include[-1] / "main/beta.h"
        header.parent.mkdir(parents=True, exist_ok=True)
        header.write_text("int alpha(void);\n")
        other.write_text("int beta(void);\n")
        steps.record(self.project, "headers", "old-input-key", steps._digests(self.project, (header, other)))
        original_other = inputs.digest(other)
        self.assertEqual(self.run_land(["us", "eu"], {"main/alpha.h": "extern int alpha(void);\n"}), "c0ffee")
        self.assertEqual(steps.altered(self.project, "headers"), [])
        self.assertEqual(steps.recorded(self.project, "headers"), "old-input-key")
        self.assertEqual(inputs.digest(other), original_other)


class ProveVersionsTests(unittest.TestCase):
    def prove(self, results: list[bool]) -> list:
        calls: list = []

        def run(host, fn, items, shared=None):
            calls.append((fn, [item[-1] for item in items]))
            return results

        project = SimpleNamespace(work=Path("/w"))
        with patch("unbake.pool.run", side_effect=run):
            land._prove_versions(project, None, project, "alpha", Path("/f.c"), ["us", "eu", "de"])
        return calls

    def test_every_version_goes_to_the_pool_once_in_version_order(self) -> None:
        self.assertEqual(self.prove([True, True, True]), [(land._builds_row, ["us", "eu", "de"])])

    def test_the_first_mismatching_version_in_order_refuses(self) -> None:
        with self.assertRaisesRegex(Held, r"land\.mismatch: alpha .* the eu ROM built"):
            self.prove([True, False, False])


class GeneratedEditTests(LandTests):
    def test_hand_edited_generated_headers_refuse_both_lands_and_clean_ones_proceed(self) -> None:
        with patch.object(land.steps, "altered", return_value=["include/span_1/code.h"]) as altered:
            for call in (
                lambda: self.run_land([]),
                lambda: land.land_original(self.project, self.host, "alpha"),
            ):
                with self.assertRaises(Held) as raised:
                    call()
                self.assertEqual(
                    raised.exception.reason,
                    "land.generated_edit: generated headers were edited by hand: include/span_1/code.h. "
                    "Put the declarations in the draft; publish places them in the shared headers",
                )
            altered.assert_called_with(self.project, "headers")
        with patch.object(land.steps, "altered", return_value=[]):
            self.assertEqual(self.run_land([]), "c0ffee")
