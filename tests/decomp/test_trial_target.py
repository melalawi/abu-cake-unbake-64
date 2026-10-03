"""Cold trial targets use the standalone Makefile without linking a ROM."""

import fcntl
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from tests.project.makefile_fixture import executable, fixture, write_rendered
from unbake.decomp.trial_target import inputs, make_target, target_object
from unbake.project import build
from unbake.project.config import Held


class TrialTargetTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.addCleanup(patch.stopall)
        self.root = Path(temporary.name).resolve()
        self.project, _policy = fixture(self.root, case=self)
        write_rendered(self.project)
        import subprocess

        from tests.helper_fixture import extraction
        from tests.process_fakes import boundary, script_output
        from unbake.decomp import trial_target

        def make(command, **kwargs):
            self.assertEqual(command[:2], ["make", "-j4"])
            self.assertIn("C_COLD=", command)
            target = Path(command[-2] if command[-1].startswith("BUILD=") else command[-1])
            if target.name == "game.ld":
                return extraction(self.project)
            target = target if target.is_absolute() else self.root / target
            target.parent.mkdir(parents=True, exist_ok=True)
            if "obj/src" in str(target):
                build.compile_object(self.project, _policy, self.project.src / "middle.c", "us", target)
                result = subprocess.CompletedProcess(command, 0, "", "")
            else:
                source = self.project.asm / "us/first.s"
                result = script_output(
                    [str(self.root / "tools/as"), "--MD", str(target.with_suffix(".d")), "-o", str(target), str(source)]
                )
            if result.returncode == 0:
                target.with_suffix(".built").touch()
            return result

        mock = boundary(trial_target, make)
        mock.start()
        self.addCleanup(mock.stop)

    def test_missing_generation_builds_inventory_and_only_requested_assembly_object(self) -> None:
        self.assertFalse((self.root / "build").exists())
        target = target_object(self.project, "first", "us")
        generation = build.current_generation(self.project, "us")
        self.assertEqual(target, generation / "obj/asm/first.o")
        self.assertEqual(target.read_bytes(), b"A")
        self.assertTrue((generation / "game.ld").is_file())
        self.assertTrue((generation / ".split.mk").is_file())
        self.assertEqual(list(generation.rglob("*.o")), [target])
        self.assertEqual((self.root / "calls").read_text().splitlines(), ["splat", "as"])
        self.assertFalse(list(generation.glob("*.elf")))
        self.assertFalse(list(generation.glob("*.z64")))

    def test_missing_c_object_builds_only_its_unit_and_recovers_leftover_receipt(self) -> None:
        target = target_object(self.project, "middle", "us")
        original = target.read_bytes()
        self.assertEqual((self.root / "calls").read_text().splitlines(), ["splat", "cc"])
        self.assertEqual(list(target.parents[2].rglob("*.o")), [target])
        target.unlink()
        self.assertTrue(target.with_suffix(".built").is_file())
        self.assertEqual(target_object(self.project, "middle", "us").read_bytes(), original)
        # The compiler cache can restore the missing object without invoking cc.
        self.assertEqual((self.root / "calls").read_text().splitlines(), ["splat", "cc"])

    def test_existing_target_does_not_run_make(self) -> None:
        target = target_object(self.project, "first", "us")
        with patch("unbake.decomp.trial_target.subprocess.run") as run:
            self.assertEqual(target_object(self.project, "first", "us"), target)
        run.assert_not_called()

    def test_published_target_inputs_do_not_acquire_writer_lock(self) -> None:
        target = target_object(self.project, "first", "us")
        # Make's extraction graph is irrelevant to reading an existing target.
        (build.current_generation(self.project, "us") / ".split.mk").unlink()
        with (self.project.build / ".lock").open("a+b") as writer:
            fcntl.flock(writer, fcntl.LOCK_EX)
            with (
                patch.object(build, "lock", side_effect=AssertionError("writer lock requested")),
                inputs(self.project, "first", ["us"]) as pinned,
            ):
                generation, actual = pinned["us"]
                self.assertEqual(actual, target)
                with (generation / ".inuse").open("a+b") as stream, self.assertRaises(BlockingIOError):
                    fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def test_missing_target_waits_for_writer_then_builds_pinned_generation(self) -> None:
        target = target_object(self.project, "first", "us")
        generation = build.current_generation(self.project, "us")
        target.unlink()
        waiting = threading.Event()
        original_lock = build.lock

        @contextmanager
        def writer_lock(project):
            waiting.set()
            with original_lock(project):
                yield

        def read():
            with inputs(self.project, "first", ["us"]) as pinned:
                return pinned["us"][1].read_bytes()

        with (self.project.build / ".lock").open("a+b") as writer, ThreadPoolExecutor(max_workers=1) as pool:
            fcntl.flock(writer, fcntl.LOCK_EX)
            with patch.object(build, "lock", side_effect=writer_lock):
                future = pool.submit(read)
                try:
                    self.assertTrue(waiting.wait(timeout=5))
                    self.assertFalse(future.done())
                    # Simulate publication while the cold target waits for the writer.
                    replacement = generation.with_name("us.1")
                    replacement.mkdir()
                    from unbake.match import publication

                    publication.swap(self.project.build_link("us"), replacement)
                finally:
                    fcntl.flock(writer, fcntl.LOCK_UN)
                self.assertEqual(future.result(timeout=10), b"A")
        self.assertTrue(target.is_file())
        self.assertFalse((replacement / "obj/asm/first.o").exists())

    def test_missing_target_is_rechecked_after_waiting_for_writer(self) -> None:
        target = target_object(self.project, "first", "us")
        target.unlink()

        @contextmanager
        def another_writer(project):
            target.write_bytes(b"other writer")
            yield

        with (
            patch.object(build, "lock", side_effect=another_writer),
            patch("unbake.decomp.trial_target.make_target", side_effect=AssertionError("already built")),
        ):
            self.assertEqual(target_object(self.project, "first", "us").read_bytes(), b"other writer")

    def test_c_target_does_not_build_other_cold_c_units(self) -> None:
        (self.root / "src/unused.c").write_text("B")
        make_target(self.project, "us", Path("build/us/game.ld"))
        generation = build.current_generation(self.project, "us")
        graph = generation / ".split.mk"
        graph.write_text(graph.read_text() + "C_OBJECTS += $(BUILD)/obj/src/unused.o\n")
        target = target_object(self.project, "middle", "us")
        self.assertEqual(list(generation.rglob("*.o")), [target])
        self.assertEqual((self.root / "calls").read_text().splitlines(), ["splat", "cc"])

    def test_inventory_build_failure_quotes_target_and_stderr(self) -> None:
        executable(self.root / "tools/splat", "import sys\nsys.stderr.write('inventory unavailable\\n')\nsys.exit(3)\n")
        with self.assertRaises(Held) as failure:
            target_object(self.project, "first", "us")
        self.assertEqual(failure.exception.phase, "try")
        self.assertIn("make -j4 VERSION=us C_COLD= build/us/game.ld", failure.exception.reason)
        self.assertIn("inventory unavailable", failure.exception.reason)
        self.assertFalse(list((self.root / "build").rglob("*.o")))

    def test_object_build_failure_quotes_target_and_stderr(self) -> None:
        target_object(self.project, "middle", "us")
        target_object_path = build.current_generation(self.project, "us") / "obj/asm/first.o"
        executable(self.root / "tools/as", "import sys\nsys.stderr.write('assembler unavailable\\n')\nsys.exit(7)\n")
        with self.assertRaises(Held) as failure:
            target_object(self.project, "first", "us")
        self.assertEqual(failure.exception.phase, "try")
        self.assertIn(f"make -j4 VERSION=us C_COLD= {target_object_path}", failure.exception.reason)
        self.assertIn("assembler unavailable", failure.exception.reason)

    def test_inventory_does_not_fall_back_to_objdiff_wrappers(self) -> None:
        target = target_object(self.project, "first", "us")
        generation = build.current_generation(self.project, "us")
        (generation / "game.ld").write_text("SECTIONS {}\n")
        (generation / "objdiff.json").write_text('{"units": [{"name": "first", "target_path": "wrapper.o"}]}')
        (generation / "wrapper.o").write_bytes(target.read_bytes())
        with self.assertRaisesRegex(Held, "obj/asm/first.o.*absent"):
            target_object(self.project, "first", "us")
