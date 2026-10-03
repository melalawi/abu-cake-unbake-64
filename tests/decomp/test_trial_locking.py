"""Trials overlap while publication and generation collection remain safe."""

import fcntl
import shutil
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import cast
from unittest.mock import patch

from tests.decomp.support import fixture
from unbake.decomp import trial, trial_target
from unbake.decomp.trial_compare import TYPES, Compare
from unbake.match import publication
from unbake.project import build
from unbake.project.config import Held, Policy, Project


class TrialLockingTests(unittest.TestCase):
    def setUp(self):
        from tests.objdiff_fixture import install

        install(self)

    def test_four_trials_overlap_and_keep_old_generation_through_storage(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            project, policy, source = fixture(root, case=self)
            old = build.current_generation(project, "us")
            old.rename(old.with_name("us.0"))
            old = old.with_name("us.0")
            project.build_link("us").unlink()
            project.build_link("us").symlink_to(old.name)
            target = old / "obj/asm/nonmatchings/alpha.o"
            barrier = threading.Barrier(5)
            release = threading.Event()
            original_target = trial_target.target_object
            observed: list[Path] = []

            def assert_writer_free() -> None:
                with (project.root / "build/.lock").open("a+b") as lock:
                    fcntl.flock(lock, fcntl.LOCK_SH | fcntl.LOCK_NB)

            def prepare(project: Project, function: str, version: str, *, generation: Path, read_only=False) -> Path:
                assert_writer_free()
                self.assertEqual(generation, old)
                return original_target(project, function, version, generation=generation, read_only=read_only)

            def compile_draft(project: object, policy: object, copied: Path, version: str, output: Path) -> Path:
                barrier.wait(timeout=10)
                self.assertTrue(release.wait(timeout=10))
                assert_writer_free()
                self.assertTrue(old.is_dir())
                shutil.copyfile(target, output)
                return output

            def diff(*args: object, generation: Path, **kwargs) -> dict[str, object]:
                assert_writer_free()
                self.assertEqual(generation, old)
                self.assertTrue(target.is_file())
                return {}

            def store(project: object, policy: object, source: Path, result: trial.Trial) -> None:
                assert_writer_free()
                with (old / ".inuse").open("a+b") as lock, self.assertRaises(BlockingIOError):
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                self.assertEqual(result.generations, {"us": old})
                observed.append(old)

            with (
                patch.object(trial_target, "target_object", side_effect=prepare),
                patch.object(trial, "compile_draft", side_effect=compile_draft),
                patch.object(trial, "diff", side_effect=diff),
                patch.object(
                    trial,
                    "compare_object",
                    side_effect=lambda *args: Compare("us", 3, 3, dict.fromkeys(TYPES, 0), [], 100, ()),
                ),
                patch.object(trial, "annotate_divergence"),
                patch.object(trial, "store_trial", side_effect=store),
                ThreadPoolExecutor(max_workers=4) as pool,
            ):
                futures = [
                    pool.submit(
                        trial.retain_draft, project, cast(Policy, policy), source, root / f"scratch-{i}", ["us"]
                    )
                    for i in range(4)
                ]
                try:
                    barrier.wait(timeout=10)
                    # Publication is possible while every candidate compile is active.
                    with build.lock(project):
                        replacement = old.with_name("us.1")
                        replacement.mkdir()
                        publication.swap(project.build_link("us"), replacement)
                    publication.collect(project)
                    self.assertTrue(old.is_dir())
                    assert_writer_free()
                finally:
                    release.set()
                for future in futures:
                    self.assertTrue(future.result(timeout=10).identical_everywhere)
            self.assertEqual(len(observed), 4)
            publication.collect(project)
            self.assertFalse(old.exists())

    def test_exception_releases_generation_pin_and_writer_lock(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            project, policy, source = fixture(root, case=self)
            generation = build.current_generation(project, "us")
            with (
                patch.object(trial, "compile_draft", side_effect=Held("compile", "failed")),
                self.assertRaisesRegex(Held, "failed"),
            ):
                trial.retain_draft(project, cast(Policy, policy), source, root / "scratch", ["us"])
            for path in (generation / ".inuse", generation.parent / ".lock"):
                with path.open("a+b") as lock:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def test_abandoned_generation_cleanup_also_honors_reader_pins(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            generation = Path(temporary).resolve() / "build/us.1"
            generation.mkdir(parents=True)
            with build.pin(generation):
                build.discard_generation(generation)
                self.assertTrue(generation.is_dir())
            build.discard_generation(generation)
            self.assertFalse(generation.exists())
