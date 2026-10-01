"""Warm clone isolation, timestamp preservation and named input refusals."""

import hashlib
import io
import shutil
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from tests.support import test_policy
from unbake.cli.main import main
from unbake.project import clone, config, toolchain


class CloneTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.live = self.root / "live"
        shutil.copytree(Path(__file__).parents[1] / "fixture", self.live)
        config_path = self.live / "config.toml"
        text = config_path.read_text()
        for index, old in enumerate(
            ("21758de98b69907bcac48b82f833f14833a5f7cc", "eafd5564f7042dd6128314aadcb802dbb9ff8cfa")
        ):
            rom = bytes.fromhex("80371240") + bytes([index]) * 4
            text = text.replace(old, hashlib.sha1(rom).hexdigest())
        config_path.write_text(text)
        (self.live / ".gitignore").write_text("build/\nasm/\nbaserom.*\ntools/cc\n")
        self.project = config.load(self.live)
        self.policy = test_policy(self.root)
        self.destination = self.root / "clone"
        for directory in (self.project.src, self.project.tools, *self.project.include):
            directory.mkdir(parents=True, exist_ok=True)
        self.external = self.root / "external"
        self.external.mkdir()
        (self.external / "cc").write_bytes(b"compiler")
        (self.project.tools / "cc").symlink_to(self.external / "cc")
        helper = self.project.tools / "helper.py"
        helper.write_text("helper")
        (self.project.tools / "compiler.sha256").write_text("0" * 64 + "  tools/helper.py\n")
        (self.live / "Makefile").write_text("check:\n\t@sha256sum -c tools/compiler.sha256\n")
        for index, name in enumerate(self.project.versions):
            version = self.project.version(name)
            rom = bytes.fromhex("80371240") + bytes([index]) * 4
            # Config fixtures carry the digest of these tiny ROMs.
            self.assertEqual(hashlib.sha1(rom).hexdigest(), version.baserom_sha1)
            external_rom = self.external / name
            external_rom.write_bytes(rom)
            version.baserom.unlink(missing_ok=True)
            version.baserom.symlink_to(external_rom)
            assembly = self.project.asm / name
            assembly.mkdir(parents=True, exist_ok=True)
            (assembly / "unit.s").write_text("assembly")
            generation = self.live / "build" / f"{name}.3"
            generation.mkdir(parents=True)
            for filename in (".split.mk", ".extract-key", "fixture.elf", f"fixture.{name}.z64"):
                (generation / filename).write_bytes(rom)
            self.project.build_link(name).symlink_to(generation.name)
            (generation / "state.json").write_text('{"warm": true}')
        self.git("init")
        self.git("add", ".")
        self.git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.test", "commit", "-m", "fixture")

    def git(self, *arguments: str) -> None:
        result = subprocess.run(["git", "-C", str(self.live), *arguments], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_warm_clone_materializes_inputs_preserves_mtimes_and_isolates_policy(self) -> None:
        before = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in self.live.rglob("*") if p.is_file()}
        with patch.object(toolchain, "verify"):
            result = clone.create(self.project, self.policy, self.destination, self.project.versions)
        self.assertEqual(result.root, self.destination)
        for source, (content, timestamp) in before.items():
            self.assertEqual((source.read_bytes(), source.stat().st_mtime_ns), (content, timestamp))
        for name in self.project.versions:
            source = self.live / "build" / f"{name}.3"
            target = self.destination / "build" / f"{name}.3"
            self.assertEqual((target / "state.json").read_bytes(), (source / "state.json").read_bytes())
            self.assertEqual(target.stat().st_mtime_ns, source.stat().st_mtime_ns)
            self.assertTrue((self.destination / "build" / name).is_symlink())
            for filename in (".split.mk", ".extract-key", "fixture.elf", f"fixture.{name}.z64"):
                self.assertEqual((target / filename).stat().st_mtime_ns, (source / filename).stat().st_mtime_ns)
                self.assertNotEqual((target / filename).stat().st_ino, (source / filename).stat().st_ino)
        self.assertFalse((result.tools / "cc").is_symlink())
        self.assertFalse(result.version("us").baserom.is_symlink())
        (result.tools / "cc").write_bytes(b"changed")
        self.assertEqual((self.external / "cc").read_bytes(), b"compiler")
        policy = config.load_policy(result.tools / "clone-policy.toml")
        self.assertTrue(policy.cache_root.is_relative_to(self.destination))
        self.assertTrue(policy.state_root.is_relative_to(self.destination))
        checked = subprocess.run(["make", "-j4", "check"], cwd=self.destination, capture_output=True, text=True)
        self.assertEqual(checked.returncode, 0, checked.stderr)

    def test_missing_or_unsafe_inputs_are_named_before_clone(self) -> None:
        cases = (
            (self.project.version("us").baserom, "baserom.us.z64"),
            (self.project.version("us").split, "fixture.yaml"),
            (self.project.tools / "compiler.sha256", "compiler.sha256"),
            (self.live / "build/us.3/.split.mk", ".split.mk"),
        )
        for path, label in cases:
            with self.subTest(label=label):
                moved = path.with_name(path.name + ".saved")
                path.rename(moved)
                try:
                    with self.assertRaisesRegex(config.Held, label), patch.object(toolchain, "verify"):
                        clone.create(self.project, self.policy, self.destination, self.project.versions)
                    self.assertFalse(self.destination.exists())
                finally:
                    moved.rename(path)
        for versions, label in (([], "version"), (["missing"], "missing"), (["us", "us"], "duplicate")):
            with self.subTest(versions=versions), self.assertRaisesRegex(config.Held, label):
                clone.create(self.project, self.policy, self.destination, versions)
        linked = self.root / "linked"
        linked.symlink_to(self.external, target_is_directory=True)
        with self.assertRaisesRegex(config.Held, "symlink"):
            clone.create(self.project, self.policy, linked / "proof", ["us"])
        with self.assertRaisesRegex(config.Held, "overlaps"):
            clone.create(self.project, self.policy, self.live / "proof", ["us"])

    def test_cli_dispatch_selects_versions_and_formats_refusal(self) -> None:
        for versions in ([], ["--version", "us"]):
            with self.subTest(versions=versions), patch.object(config, "load_policy", return_value=self.policy):
                with patch.object(toolchain, "verify"), redirect_stdout(io.StringIO()) as output:
                    code = main(["--project", str(self.live), "clone", str(self.destination), *versions])
                self.assertEqual(code, 0)
                self.assertIn("OK(clone)", output.getvalue())
                self.assertTrue((self.destination / "build/us").is_symlink())
                self.assertEqual((self.destination / "build/us-rev1").exists(), not versions)
                shutil.rmtree(self.destination)
        with patch.object(config, "load_policy", return_value=self.policy), redirect_stderr(io.StringIO()) as error:
            code = main(["--project", str(self.live), "clone", str(self.destination), "--version", "missing"])
        self.assertEqual(code, 1)
        self.assertIn("missing", error.getvalue())
