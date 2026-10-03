"""Warm clone isolation, timestamp preservation and named input refusals."""

import hashlib
import io
import json
import os
import shutil
import tarfile
import tempfile
import unittest
from collections.abc import Iterator
from contextlib import redirect_stdout
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from tests.support import test_policy
from unbake.cli.main import main
from unbake.project import build, clone, config, hygiene, toolchain


class CloneTests(unittest.TestCase):
    def setUp(self) -> None:
        guidance = patch("unbake.cli.guidance.resolve", return_value="unbake next")
        guidance.start()
        self.addCleanup(guidance.stop)
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.live = self.root / "live"
        shutil.copytree(Path(__file__).parents[1] / "fixture", self.live)
        from tests.git_fixture import Index
        from tests.process_fakes import boundary

        self.index = Index(self.live)
        for mock in (boundary(clone, self.index.run), boundary(hygiene, self.index.run)):
            mock.start()
            self.addCleanup(mock.stop)
        config_path = self.live / "config.toml"
        text = config_path.read_text()
        for index, old in enumerate(
            ("21758de98b69907bcac48b82f833f14833a5f7cc", "eafd5564f7042dd6128314aadcb802dbb9ff8cfa")
        ):
            rom = bytes.fromhex("80371240") + bytes([index]) * 4
            text = text.replace(old, hashlib.sha1(rom).hexdigest())
        config_path.write_text(text)
        (self.live / ".gitignore").write_text("build/\nasm/\nroms/\ntools/cc\n")
        self.project = config.load(self.live)
        (self.live / ".gitignore").write_text(hygiene.ignore_text(self.project))
        self.project.roms.mkdir(exist_ok=True)
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
            version.baserom.write_bytes(external_rom.read_bytes())
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
        if arguments[0] == "init":
            (self.live / ".git").mkdir(exist_ok=True)
            self.index.entries.clear()
        elif arguments[0] == "add":
            for path in self.live.rglob("*"):
                relative = path.relative_to(self.live)
                if (
                    path.is_file()
                    and not any(name in relative.parts for name in (".git", "build", "roms", "asm"))
                    and relative.as_posix() != "tools/cc"
                ):
                    self.index.add(relative.as_posix())
        else:
            self.assertIn("commit", arguments)

    def test_warm_clone_materializes_inputs_preserves_mtimes_and_isolates_policy(self) -> None:
        before = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in self.live.rglob("*") if p.is_file()}
        with patch.object(clone, "prepare", return_value=False):
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
        self.assertIn(
            hashlib.sha256((result.tools / "helper.py").read_bytes()).hexdigest(),
            (result.tools / "compiler.sha256").read_text(),
        )

    def test_published_objects_share_storage_but_replacement_is_isolated(self) -> None:
        source = self.live / "build/us.3/obj/src/unit.o"
        source.parent.mkdir(parents=True)
        source.write_bytes(b"published object")
        asset = self.live / "build/us.3/obj/assets/data.bin.o"
        asset.parent.mkdir(parents=True)
        asset.write_bytes(b"mutable asset object")
        with (
            patch.object(clone, "prepare", return_value=False),
            patch.object(clone.fcntl, "ioctl", side_effect=OSError),
        ):
            clone.create(self.project, self.policy, self.destination, ["us"])
        target = self.destination / "build/us.3/obj/src/unit.o"
        self.assertEqual(source.stat().st_ino, target.stat().st_ino)
        replacement = target.with_suffix(".o.partial")
        replacement.write_bytes(b"new object")
        replacement.replace(target)
        self.assertEqual(source.read_bytes(), b"published object")
        self.assertEqual(target.read_bytes(), b"new object")
        copied_asset = self.destination / "build/us.3/obj/assets/data.bin.o"
        self.assertNotEqual(copied_asset.stat().st_ino, asset.stat().st_ino)
        copied_asset.write_bytes(b"changed asset object")
        self.assertEqual(asset.read_bytes(), b"mutable asset object")

    def test_rom_and_graph_contents_are_streamed(self) -> None:
        evidence = self.project.build / "setup"
        evidence.mkdir()
        prefix = b'{"padding":"'
        middle = b'","workspace_id":"'
        padding = b"x" * (1024 * 1024 - 18 - len(prefix) - len(middle))
        layout = evidence / "layout.json"
        layout.write_bytes(prefix + padding + middle + self.project.workspace_id.encode() + b'"}')
        (evidence / "us.json").write_bytes(b'{"providers": []}')
        (evidence / "symbol-proposal.json").write_bytes(b"unread review")
        (evidence / "proof-old").mkdir()
        original_read = Path.read_bytes
        roms = {self.project.version(v).baserom for v in self.project.versions}

        def read(path):
            if path in roms or path.name == ".split.mk" or path.parent.name == "setup":
                self.fail(f"clone loaded whole input: {path}")
            return original_read(path)

        with patch.object(Path, "read_bytes", read), patch.object(clone, "prepare", return_value=False):
            result = clone.create(self.project, self.policy, self.destination, self.project.versions)
        copied = result.build / "setup"
        self.assertEqual(json.loads((copied / "layout.json").read_bytes())["workspace_id"], result.workspace_id)
        self.assertEqual((copied / "us.json").read_bytes(), b'{"providers": []}')
        self.assertFalse((copied / "symbol-proposal.json").exists())
        self.assertFalse((copied / "proof-old").exists())
        self.assertIn(self.project.workspace_id.encode(), layout.read_bytes())

    def test_warm_compiler_pins_do_not_reinstall_into_clone_cache(self) -> None:
        from unbake.project import makefile, setup

        setup.publish_files(self.project, makefile.helpers(self.project))
        with (
            patch.object(clone.compiler_files, "sha", return_value="pinned"),
            patch.object(clone.toolchain, "specification", return_value=type("Spec", (), {"pins": {"cc": "pinned"}})),
            patch.object(clone.toolchain, "ensure", side_effect=AssertionError("warm clone reinstall")),
        ):
            for ident in self.project.compilers:
                path = self.project.tools / ident / "cc"
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"pinned")
            self.assertFalse(clone.prepare(self.project, self.policy))

    def test_ready_shell_without_initial_commit_keeps_durable_inputs(self) -> None:
        shutil.rmtree(self.live / ".git")
        self.git("init")
        evidence = self.live / "docs/setup/owner.json"
        evidence.parent.mkdir(parents=True)
        evidence.write_text('{"owner": "alpha"}\n')
        with patch.object(clone, "prepare", return_value=False):
            result = clone.create(self.project, self.policy, self.destination, self.project.versions)
        self.assertEqual(result.id, self.project.id)
        self.assertNotEqual(result.workspace_id, self.project.workspace_id)
        self.assertEqual((self.destination / "Makefile").read_bytes(), (self.live / "Makefile").read_bytes())
        self.assertEqual((self.destination / "docs/setup/owner.json").read_bytes(), evidence.read_bytes())

    def test_cli_acquires_missing_and_stale_compiler_without_changing_source(self) -> None:
        content = b"pinned compiler"
        pin = hashlib.sha256(content).hexdigest()
        archive = self.root / "compiler.tar.gz"
        with tarfile.open(archive, "w:gz") as output:
            for name in ("cc", "as1"):
                entry = tarfile.TarInfo(name)
                entry.size = len(content)
                output.addfile(entry, io.BytesIO(content))
        spec = replace(
            toolchain.specification("ido-7.1"),
            pins={"cc": pin, "as1": pin},
            downloads=(
                toolchain.Download(archive.as_uri(), hashlib.sha256(archive.read_bytes()).hexdigest(), ("cc", "as1")),
            ),
        )
        for stale in (True, False):
            with self.subTest(stale=stale):
                directory = self.project.tools / spec.id
                directory.mkdir(exist_ok=True)
                for name in spec.pins:
                    target = directory / name
                    if stale:
                        target.write_bytes(b"obsolete compiler")
                    else:
                        target.unlink(missing_ok=True)
                warm_receipt = self.live / "build/us.3/obj/src/unit.built"
                warm_receipt.parent.mkdir(parents=True, exist_ok=True)
                warm_receipt.touch()
                before = {path: path.read_bytes() for path in directory.iterdir()}
                with (
                    patch.object(toolchain, "registry", return_value={spec.id: spec}),
                    patch.dict(os.environ),
                    redirect_stdout(io.StringIO()) as output,
                ):
                    policy_path = self.root / "policy.toml"
                    policy_path.write_text(clone.isolated_policy(self.policy, self.root / "unused"))
                    code = main(
                        ["--policy", str(policy_path), "--project", str(self.live), "clone", str(self.destination)]
                    )
                self.assertEqual(code, 0, output.getvalue())
                self.assertIn("OK(clone)", output.getvalue())
                self.assertEqual(before, {path: path.read_bytes() for path in directory.iterdir()})
                toolchain.verify(self.destination / "tools" / spec.id, spec)
                self.assertTrue(warm_receipt.is_file())
                self.assertFalse((self.destination / "build/us.3/obj/src/unit.built").exists())
                helper = self.destination / "tools/cache.py"
                self.assertIn("mode = path.stat().st_mode", helper.read_text())
                self.assertNotIn(
                    "if path.exists():", helper.read_text().split("def get(", 1)[1].split("def _temporary", 1)[0]
                )
                self.assertTrue((self.destination / "Makefile").is_file())
                shutil.rmtree(self.destination)
        self.assertFalse(list(self.root.glob(".clone-*")))

    def test_failed_preparation_never_publishes_partial_checkout(self) -> None:
        with (
            patch.object(clone, "prepare", side_effect=config.Held("setup", "pinned download refused")),
            self.assertRaisesRegex(config.Held, "pinned download refused"),
        ):
            clone.create(self.project, self.policy, self.destination, self.project.versions)
        self.assertFalse(self.destination.exists())
        self.assertFalse(list(self.root.glob(".clone-*")))

    def test_cli_names_policy_and_clone_form(self) -> None:
        for operands in (["clone", "SRC", "DEST"], ["--project", str(self.live), "clone", "SRC", "DEST"]):
            with redirect_stdout(io.StringIO()) as output:
                code = main(operands)
            self.assertEqual(code, 1)
            self.assertIn("CLI form: unbake [--project SRC] clone DEST", output.getvalue())
        with (
            patch.dict(os.environ, {"UNBAKE_POLICY": "", "XDG_CONFIG_HOME": str(self.root / "absent")}),
            redirect_stdout(io.StringIO()) as output,
        ):
            code = main(["--project", str(self.live), "clone", str(self.destination)])
        self.assertEqual(code, 1)
        self.assertIn("policy.cache_root", output.getvalue())
        self.assertTrue((self.root / "absent/unbake/policy.toml").is_file())

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
                    with self.assertRaisesRegex(config.Held, label), patch.object(clone, "prepare", return_value=False):
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

    def test_clone_pins_generation_and_skips_vanishing_build_scratch(self) -> None:
        generation = self.live / "build/us.3"
        assembly = self.project.asm / "us"
        assembly.rename(generation / "assembly")
        assembly.symlink_to(self.project.build_link("us") / "assembly", target_is_directory=True)
        replacement = self.live / "build/us.4"
        shutil.copytree(generation, replacement)
        (replacement / "assembly/unit.s").write_text("replacement assembly")
        (replacement / "state.json").write_text('{"replacement": true}')
        scratch = []
        for parent in (generation, generation / "obj/src", generation / "assembly"):
            parent.mkdir(parents=True, exist_ok=True)
            for name in (".extract-gone", ".object-gone", ".compile-gone"):
                path = parent / name
                path.mkdir()
                (path / "temporary.s").write_text("scratch")
                scratch.append(path)
            for name in (".input-gone.s", "unit.o.partial"):
                path = parent / name
                path.write_text("scratch")
                scratch.append(path)
        resolve = Path.resolve
        iterdir = Path.iterdir

        def publish(path: Path, strict: bool = False) -> Path:
            pinned = resolve(path, strict=strict)
            if path == assembly:
                link = self.project.build_link("us")
                link.unlink()
                link.symlink_to(replacement.name, target_is_directory=True)
            return pinned

        def vanish(path: Path) -> Iterator[Path]:
            children = list(iterdir(path))
            for child in children:
                if child in scratch:
                    if child.is_dir():
                        shutil.rmtree(child)
                    else:
                        child.unlink()
            return iter(children)

        with (
            patch.object(clone, "prepare", return_value=False),
            patch.object(Path, "resolve", publish),
            patch.object(Path, "iterdir", vanish),
        ):
            clone.create(self.project, self.policy, self.destination, ["us"])
        target = self.destination / "build/us.3"
        self.assertEqual((self.destination / "asm/us/unit.s").read_text(), "assembly")
        self.assertEqual((target / "state.json").read_text(), '{"warm": true}')
        self.assertEqual((self.destination / "build/us").readlink(), Path("us.3"))
        self.assertFalse((self.destination / "build/us.4").exists())
        self.assertTrue((target / ".extract-key").is_file())
        for path in scratch:
            self.assertFalse((target / path.relative_to(generation)).exists())

    def test_cli_keeps_source_generation_pinned_through_copy(self) -> None:
        generation = self.live / "build/us.3"
        original_copy = clone.copy_regular
        attempted = []

        def copy(source: Path, target: Path, ancestors: frozenset[Path] = frozenset(), **kwargs) -> None:
            if source == generation:
                build.discard_generation(generation)
                attempted.append(generation)
                self.assertTrue(generation.is_dir())
            original_copy(source, target, ancestors, **kwargs)

        with (
            patch.object(config, "load_policy", return_value=self.policy),
            patch.object(clone, "prepare", return_value=False),
            patch.object(clone, "copy_regular", side_effect=copy),
            redirect_stdout(io.StringIO()),
        ):
            code = main(["--project", str(self.live), "clone", str(self.destination), "--version", "us"])
        self.assertEqual(code, 0)
        self.assertEqual(attempted, [generation])
        self.assertTrue((self.destination / "build/us.3/.inuse").is_file())

    def test_local_policy_and_build_outputs_leave_tracked_checkout_clean(self) -> None:
        helper = self.project.tools / "helper.py"
        (self.project.tools / "compiler.sha256").write_text(
            hashlib.sha256(helper.read_bytes()).hexdigest() + "  tools/helper.py\n"
        )
        (self.live / "Makefile").write_text(
            "include build/us/.split.mk\n"
            'check:\n\t@test "$$UNBAKE_POLICY" = "$(CURDIR)/tools/clone-policy.toml"\n'
            "\t@mkdir -p .unbake/cache .unbake/state\n"
            "\t@touch .unbake/cache/output .unbake/state/output\n"
        )
        for name in self.project.versions:
            (self.live / "build" / f"{name}.3/.split.mk").write_text("# warm graph\n")
        self.git("add", ".")
        self.git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.test", "commit", "-m", "clean inputs")
        with patch.object(clone, "prepare", return_value=False):
            cloned = clone.create(self.project, self.policy, self.destination, self.project.versions)
        self.assertEqual(cloned.id, self.project.id)
        self.assertNotEqual(cloned.workspace_id, self.project.workspace_id)
        self.assertIn("/.unbake/", (self.destination / ".gitignore").read_text())
        self.assertIn("/tools/clone-policy.toml", (self.destination / ".gitignore").read_text())
        self.assertEqual((self.destination / "Makefile").read_bytes(), (self.live / "Makefile").read_bytes())

    def test_cli_dispatch_selects_versions_and_formats_refusal(self) -> None:
        for versions in ([], ["--version", "us"]):
            with self.subTest(versions=versions), patch.object(config, "load_policy", return_value=self.policy):
                with patch.object(clone, "prepare", return_value=False), redirect_stdout(io.StringIO()) as output:
                    code = main(["--project", str(self.live), "clone", str(self.destination), *versions])
                self.assertEqual(code, 0)
                self.assertIn("OK(clone)", output.getvalue())
                self.assertTrue((self.destination / "build/us").is_symlink())
                self.assertEqual((self.destination / "build/us-rev1").exists(), not versions)
                shutil.rmtree(self.destination)
        with patch.object(config, "load_policy", return_value=self.policy), redirect_stdout(io.StringIO()) as error:
            code = main(["--project", str(self.live), "clone", str(self.destination), "--version", "missing"])
        self.assertEqual(code, 1)
        self.assertIn("missing", error.getvalue())
