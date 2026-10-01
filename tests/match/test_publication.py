"""Match behavior with stored trial proofs and a controlled build implementation."""

import fcntl
import hashlib
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest.mock import patch

from tests.match.support import MatchFixture
from unbake.match import publication, staging
from unbake.match import queue as match
from unbake.project.config import Held


class PublicationTests(MatchFixture):
    def test_retained_artifacts_are_not_staged_or_publication_inputs(self) -> None:
        artifacts = self.root / "artifacts" / "candidate-trials"
        artifacts.mkdir(parents=True)
        retained = artifacts / "draft.c"
        retained.write_text("retained trial\n")
        self.queue("alpha")

        def during_build(tree: Path, generation_for: Callable[[str], Path]) -> None:
            self.assertFalse((tree / "artifacts").exists())
            self.assertTrue((tree / "include" / "types.h").exists())
            staged_source = tree / "src" / "alpha.c"
            self.assertNotEqual(staged_source.stat().st_ino, (self.sources / "alpha.c").stat().st_ino)
            retained.write_text("new retained trial during landing\n")

        self.on_build = during_build
        receipts = match.run(self.project, self.policy)
        self.assertTrue(any(line.startswith("OK(match): alpha") for line in receipts))
        self.assertEqual(retained.read_text(), "new retained trial during landing\n")
        before = staging.fingerprint(self.root)
        (self.root / "include" / "types.h").write_text("typedef long word;\n")
        self.assertNotEqual(before, staging.fingerprint(self.root))

    def test_success_moves_sources_flips_rows_records_and_collects(self) -> None:
        self.queue("alpha", "beta")
        expected = {function: (self.sources / f"{function}.c").read_bytes() for function in ("alpha", "beta")}
        with patch.object(staging.subprocess, "run", wraps=staging.subprocess.run) as copy:
            receipts = match.run(self.project, self.policy)
        self.assertEqual(self.calls, [("alpha", "beta")])
        self.assertTrue(all(line.startswith("OK(match):") for line in receipts))
        self.assertEqual(len(receipts), 2)
        self.assertTrue(all(call.args[0][:3] == ["cp", "-a", "--reflink=auto"] for call in copy.call_args_list))
        self.assertEqual(len(copy.call_args_list), 2)
        for function, content in expected.items():
            self.assertEqual((self.src / f"{function}.c").read_bytes(), content)
            self.assertTrue((self.sources / f"{function}.c").exists())
        for version in self.versions:
            self.assertFalse(self.original[version].exists())
            self.assertEqual(self.current(self.project, version).name, f"{version}.1")
            text = self.project.version(version).split.read_text()
            self.assertIn("- [0x1000, c, alpha]", text)
            self.assertIn("- [0x1020, asm, gamma]", text)
            self.assertIn("- [0x1030, data, constants]", text)
        self.assertEqual(self.queued(), [])
        rows = self.matched()
        self.assertEqual({row["function"] for row in rows}, {"alpha", "beta"})
        for row in rows:
            self.assertEqual(row["versions"], list(self.versions))
            self.assertEqual(row["sha256"], hashlib.sha256(expected[row["function"]]).hexdigest())
            self.assertIn("at", row)

    def test_failure_is_bisected_and_passing_remainder_is_rebuilt(self) -> None:
        self.queue("alpha", "beta", "gamma")
        self.fail.add(("beta", "eu"))
        receipts = match.run(self.project, self.policy)
        self.assertTrue(any(line.startswith("HELD(match): beta:") and "eu" in line for line in receipts))
        self.assertEqual(self.calls[0], ("alpha", "beta", "gamma"))
        self.assertEqual(self.calls[-1], ("alpha", "gamma"))
        self.assertEqual({row["function"] for row in self.matched()}, {"alpha", "gamma"})
        self.assertEqual([row["function"] for row in self.queued()], ["beta"])
        self.assertTrue((self.sources / "beta.c").exists())
        self.assertFalse((self.src / "beta.c").exists())
        self.assertIn(", asm, beta]", self.project.version("eu").split.read_text())

    def test_batch_interaction_is_refused_by_name(self) -> None:
        self.queue("alpha", "beta", "gamma")
        self.interactions.append({"alpha", "beta"})
        receipts = match.run(self.project, self.policy)
        self.assertTrue(any(line.startswith("HELD(match): beta:") for line in receipts))
        self.assertEqual({row["function"] for row in self.matched()}, {"alpha", "gamma"})
        self.assertEqual(self.calls[-1], ("alpha", "gamma"))

    def test_all_build_failures_leave_live_tree_untouched(self) -> None:
        self.queue("alpha", "beta")
        self.fail.update({("alpha", "us"), ("beta", "eu")})
        receipts = match.run(self.project, self.policy)
        self.assertEqual(len(receipts), 2)
        self.assertTrue(all(line.startswith("HELD(match):") for line in receipts))
        self.assert_untouched()
        self.assertEqual(len(self.queued()), 2)
        self.assertFalse(list((self.root / "build").glob("us.[1-9]*")))
        self.assertFalse(list((self.root / "build").glob("eu.[1-9]*")))

    def test_static_failure_does_not_block_other_queued_functions(self) -> None:
        self.queue("alpha", "beta")
        self.remove_proofs("alpha")
        receipts = match.run(self.project, self.policy)
        self.assertTrue(any(line.startswith("HELD(match): alpha:") for line in receipts))
        self.assertTrue(any(line.startswith("OK(match): beta") for line in receipts))
        self.assertEqual(self.calls, [("beta",)])
        self.assertEqual([row["function"] for row in self.queued()], ["alpha"])

    def test_try_completes_during_build_and_held_generation_survives_swap(self) -> None:
        self.queue("alpha")
        events = []
        trial_hold = (self.original["us"] / ".inuse").open("rb")
        self.addCleanup(trial_hold.close)
        fcntl.flock(trial_hold, fcntl.LOCK_SH)

        def while_building(tree: Path, generation_for: Callable[[str], Path]) -> Any:
            with (self.root / "build" / ".lock").open("a+b") as final_lock:
                fcntl.flock(final_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                events.append("final lock is free")
            generation = self.current(self.project, "us")
            with (generation / ".inuse").open("rb") as simulated_try:
                fcntl.flock(simulated_try, fcntl.LOCK_SH | fcntl.LOCK_NB)
                output = (generation / "object.o").read_bytes()
                scratch = Path(self.temporary.name) / "trial"
                scratch.mkdir()
                (scratch / "compares.json").write_text(
                    json.dumps({"identical": output == b"original immutable output"})
                )
                events.append("try completed")
            self.assertEqual(generation_for("us").parent, self.root / "build")
            self.assertEqual(self.current(self.project, "us"), self.original["us"])
            self.assertFalse((self.src / "alpha.c").exists())

        self.on_build = while_building
        receipts = match.run(self.project, self.policy)
        self.assertEqual(events, ["final lock is free", "try completed"])
        self.assertTrue(receipts[0].startswith("OK(match): alpha"))
        self.assertTrue(self.original["us"].exists())
        self.assertEqual((self.original["us"] / "object.o").read_bytes(), b"original immutable output")
        self.assertFalse(self.original["eu"].exists())
        fcntl.flock(trial_hold, fcntl.LOCK_UN)
        trial_hold.close()
        self.on_build = None
        self.assertEqual(match.run(self.project, self.policy), [])
        self.assertFalse(self.original["us"].exists())

    def test_final_writes_and_swaps_hold_project_lock(self) -> None:
        self.queue("alpha")
        original_swap = publication.swap
        observed = []

        def checked_swap(link: Path, target: Path) -> Any:
            with (self.root / "build" / ".lock").open("a+b") as other, self.assertRaises(BlockingIOError):
                fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
            observed.append(link.name)
            original_swap(link, target)

        with patch.object(publication, "swap", checked_swap):
            match.run(self.project, self.policy)
        self.assertEqual(observed, list(self.versions))

    def test_concurrent_input_edit_refuses_publication(self) -> None:
        self.queue("alpha")
        self.on_build = lambda tree, generation_for: (self.root / "include" / "types.h").write_text(
            "typedef long word;\n"
        )
        receipts = match.run(self.project, self.policy)
        self.assertTrue(any("inputs changed" in line and "include/types.h" in line for line in receipts))
        self.assert_untouched()
        self.assertEqual(len(self.queued()), 1)

    def test_withdraw_during_build_prevents_publication(self) -> None:
        self.queue("alpha")
        self.on_build = lambda tree, generation_for: match.withdraw("alpha", project=self.project)
        receipts = match.run(self.project, self.policy)
        self.assertTrue(any("alpha" in line and "withdrawn" in line for line in receipts))
        self.assert_untouched()
        self.assertEqual(self.queued(), [])

    def test_generation_change_during_build_refuses_publication(self) -> None:
        self.queue("alpha")

        def change_generation(tree: Path, generation_for: Callable[[str], Path]) -> Any:
            generation = self.root / "build" / "us.50"
            generation.mkdir()
            (generation / ".inuse").touch()
            (generation / "object.o").write_bytes(b"other publisher output")
            publication.swap(self.project.build_link("us"), generation)

        self.on_build = change_generation
        receipts = match.run(self.project, self.policy)
        self.assertTrue(any("us" in line and "generation changed" in line for line in receipts))
        self.assertEqual(self.current(self.project, "us").name, "us.50")
        self.assertFalse((self.src / "alpha.c").exists())
        self.assertEqual(self.matched(), [])

    def test_missing_build_result_is_refused(self) -> None:
        self.queue("alpha")
        with patch.object(match.build, "build", return_value={}):
            receipts = match.run(self.project, self.policy)
        self.assertTrue(any("alpha" in line and "missing VERSION us result" in line for line in receipts))
        self.assert_untouched()

    def test_final_write_failure_rolls_back_tree_links_ledger_and_queue(self) -> None:
        self.queue("alpha")
        original_swap = publication.swap

        def fail_second_swap(link: Path, target: Path) -> Any:
            if link.name == "eu" and target != self.original["eu"]:
                raise OSError("eu symlink swap refused")
            original_swap(link, target)

        with (
            patch.object(publication, "swap", fail_second_swap),
            self.assertRaisesRegex(Held, "eu symlink swap refused"),
        ):
            match.run(self.project, self.policy)
        self.assert_untouched()
        self.assertTrue((self.sources / "alpha.c").exists())
        self.assertEqual(len(self.queued()), 1)

    def test_queue_corruption_names_missing_value(self) -> None:
        directory = self.root / "data"
        directory.mkdir()
        (directory / "match-queue.jsonl").write_text('{"function":"alpha"}\n')
        with self.assertRaisesRegex(Held, "match-queue.jsonl.*missing source"):
            match.status(project=self.project)

    def test_external_named_source_replaces_existing_partial(self) -> None:
        source = self.draft("alpha")
        external = Path(self.temporary.name) / "alpha.c"
        external.write_bytes(source.read_bytes())
        match.submit(self.project, self.policy, external)
        (self.src / "alpha.c").write_text("#ifdef NON_MATCHING\nint alpha(void) { return 1; }\n#endif\n")
        receipts = match.run(self.project, self.policy)
        self.assertTrue(receipts[0].startswith("OK(match):"))
        self.assertEqual((self.src / "alpha.c").read_bytes(), external.read_bytes())
        self.assertTrue(source.exists())
        self.assertTrue(external.exists())
