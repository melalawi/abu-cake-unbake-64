import fcntl
import tempfile
import threading
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from tests.project.makefile_fixture import WORK, fixture
from unbake.match import publication
from unbake.project import build, config, setup


class BuildTests(unittest.TestCase):
    def test_compile_versions_shares_all_slots_and_preserves_failures(self):
        sources = [Path(f"src/unit{index}.c") for index in range(8)]
        jobs = {version: (sources, Path(version)) for version in ("us", "eu")}
        first_wave = threading.Barrier(4)
        mutex = threading.Lock()
        visited = []
        active = peak = 0
        policy = config.load_policy()
        from dataclasses import replace

        policy = replace(policy, cores=4)

        def compile_chunk(project, policy, members, version, out):
            nonlocal active, peak
            with mutex:
                active += 1
                peak = max(peak, active)
                visited.append((version, tuple(members)))
                initial = len(visited) <= 4
            if initial:
                first_wave.wait(timeout=5)
            with mutex:
                active -= 1
            return {source.stem: "diagnostic" for source in members if source.stem == "unit3"}

        with patch.object(build, "compile_objects", side_effect=compile_chunk):
            result = build.compile_versions(None, policy, jobs)
        self.assertEqual(peak, 4)
        self.assertEqual({version for version, _ in visited[:4]}, {"us", "eu"})
        self.assertEqual({v: {"unit3": "diagnostic"} for v in jobs}, result)
        self.assertCountEqual([(v, p) for v, chunk in visited for p in chunk], [(v, p) for v in jobs for p in sources])

    def setUp(self) -> None:
        WORK.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=WORK)
        self.addCleanup(self.temporary.cleanup)
        self.addCleanup(patch.stopall)
        self.root = Path(self.temporary.name).resolve()
        self.project, self.policy = fixture(self.root, case=self)

    def test_generation_requires_existing_symlink(self) -> None:
        with self.assertRaisesRegex(config.Held, "symlink"):
            build.current_generation(self.project, "us")
        generation = self.root / "build/us.1"
        generation.mkdir(parents=True)
        self.project.build_link("us").symlink_to("us.1")
        self.assertEqual(build.current_generation(self.project, "us"), generation)
        self.project.build_link("us").unlink()
        self.project.build_link("us").symlink_to("gone")
        with self.assertRaises(config.Held):
            build.current_generation(self.project, "us")

    def test_pin_current_retries_collection_during_resolution(self) -> None:
        old = self.root / "build/us.0"
        new = self.root / "build/us.1"
        old.mkdir(parents=True)
        new.mkdir()
        publication.swap(self.project.build_link("us"), old)
        publication.swap(self.project.build_link("us"), new)
        publication.collect(self.project)
        self.assertFalse(old.exists())
        with (
            patch.object(build, "current_generation", side_effect=[config.Held("build", "collected"), new]),
            build.pin_current(self.project, "us") as pinned,
        ):
            self.assertEqual(pinned, new)
        self.project.build_link("us").unlink()
        with (
            patch.object(build, "current_generation", side_effect=config.Held("build", "invalid")),
            self.assertRaisesRegex(config.Held, "invalid"),
            build.pin_current(self.project, "us"),
        ):
            self.fail("missing published generation was accepted")

    def test_pin_current_retries_publication_before_and_after_pin(self) -> None:
        for collected in (False, True):
            with self.subTest(collected=collected):
                old = self.root / f"build/us.{int(collected) * 2}"
                new = old.with_name(f"us.{int(collected) * 2 + 1}")
                old.mkdir(parents=True)
                new.mkdir()
                publication.swap(self.project.build_link("us"), old)
                original_pin = build.pin
                attempts = []

                @contextmanager
                def racing_pin(
                    generation, old=old, new=new, collected=collected, attempts=attempts, original_pin=original_pin
                ):
                    attempts.append(generation)
                    if generation == old and collected:
                        publication.swap(self.project.build_link("us"), new)
                        publication.collect(self.project)
                    with original_pin(generation):
                        if generation == old and not collected:
                            publication.swap(self.project.build_link("us"), new)
                            publication.collect(self.project)
                            self.assertTrue(old.is_dir())
                        yield generation

                with (
                    patch.object(build, "pin", side_effect=racing_pin),
                    build.pin_current(self.project, "us") as pinned,
                ):
                    self.assertEqual(pinned, new)
                    self.assertEqual(attempts, [old, new])
                    with (new / ".inuse").open("a+b") as stream, self.assertRaises(BlockingIOError):
                        fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    if old.exists():
                        with (old / ".inuse").open("a+b") as stream:
                            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                publication.collect(self.project)
                self.assertFalse(old.exists())

    def test_make_proves_generation_and_records_comparison_failure(self) -> None:
        import subprocess

        from tests.process_fakes import boundary

        setup.run(self.project, self.policy)
        generation = self.root / "build/us.1"
        for passed in (True, False):

            def run(command, passed=passed, **kwargs):
                self.assertEqual(command[:2], ["make", "-f"])
                self.assertEqual(command[-2:], ["-j2", "-k"])
                driver = Path(command[2]).read_text()
                self.assertIn("VERSION=us", driver)
                self.assertIn("COMPARE=1", driver)
                self.assertIn(f"BUILD={generation}", driver)
                generation.mkdir(parents=True, exist_ok=True)
                (generation / "build.log").write_text(
                    str(generation / "game.us.z64") + (": OK\n" if passed else ": FAILED\n")
                )
                (generation / "build.exit").write_text("0" if passed else "1")
                return subprocess.CompletedProcess(command, int(not passed), "", "")

            with boundary(build, run):
                result = build.build(
                    self.project, self.policy, ["us"], tree=self.root, generation_for=lambda v: generation
                )["us"]
            self.assertEqual(result.ok, passed)
            self.assertEqual(result.generation, generation)
            self.assertTrue(result.sha1_line.endswith(": OK" if passed else ": FAILED"))

    def test_compile_cache_uses_preprocessed_headers_and_compiler_digest(self) -> None:
        setup.run(self.project, self.policy)
        source = self.root / "src/middle.c"
        out = self.root / "scratch/middle.o"
        build.compile_object(self.project, self.policy, source, "us", out)
        out.unlink()
        build.compile_object(self.project, self.policy, source, "us", out)
        self.assertEqual((self.root / "calls").read_text().splitlines(), ["cc"])
        self.assertTrue(out.read_bytes().startswith(b"\x7fELF"))
        (self.root / "include/value.h").write_text("#define VALUE 2\n")
        build.compile_object(self.project, self.policy, source, "us", out)
        self.assertEqual((self.root / "calls").read_text().splitlines(), ["cc", "cc"])
        pins = self.project.compilers["fixture"].sha256
        pins.write_text(pins.read_text() + "# another compiler recipe\n")
        build.compile_object(self.project, self.policy, source, "us", out)
        self.assertEqual((self.root / "calls").read_text().splitlines(), ["cc", "cc"])
        self.assertTrue((self.policy.cache_root / "cc").is_dir())

    def test_sn64_cached_object_uses_same_flags_and_pipeline(self) -> None:
        project, policy = fixture(self.root, "sn64", case=self)
        setup.run(project, policy)
        source = self.root / "src/middle.c"
        source.write_text('#include "value.h"\nint middle(void) { return VALUE; }\n')
        out = self.root / "scratch/middle.o"
        build.compile_object(project, policy, source, "us", out)
        first = out.read_bytes()
        build.compile_object(project, policy, source, "us", out)
        self.assertEqual(out.read_bytes(), first)
        self.assertEqual((self.root / "calls").read_text().splitlines(), ["cc1", "as"])
        self.assertIn(b"\r\n", first)
        self.assertNotIn(b".rodata", first)

    def test_unknown_version_and_missing_source_name_the_value(self) -> None:
        with self.assertRaisesRegex(config.Held, "unknown VERSION"):
            build.compile_object(self.project, self.policy, self.root / "missing.c", "unknown", self.root / "out.o")
        with self.assertRaisesRegex(config.Held, "missing.c"):
            build.compile_object(self.project, self.policy, self.root / "missing.c", "us", self.root / "out.o")

    def test_zero_exit_without_sha1_is_not_proof(self) -> None:
        setup.run(self.project, self.policy)
        with patch("unbake.project.build.subprocess.run") as run:
            run.return_value.returncode = 0
            run.return_value.stdout = "nothing to do\n"
            result = build.build(
                self.project, self.policy, ["us"], tree=self.root, generation_for=lambda v: self.root / "build/us.1"
            )["us"]
        self.assertFalse(result.ok)
        self.assertEqual(result.sha1_line, "")
