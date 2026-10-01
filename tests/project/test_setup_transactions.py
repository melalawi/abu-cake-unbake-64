"""Setup proves isolated inputs and rolls back every publication mutation."""

import hashlib
import os
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import toml

from tests.project.makefile_fixture import WORK, fixture
from unbake.project import config, fingerprint, setup, setup_proof


class SetupTransactionTests(unittest.TestCase):
    def setUp(self) -> None:
        WORK.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=WORK)
        self.addCleanup(self.temporary.cleanup)
        self.addCleanup(patch.stopall)
        self.root = Path(self.temporary.name)
        self.project, self.policy = fixture(self.root)
        data = tomllib.loads((self.root / "config.toml").read_text())
        data.update(
            schema=1,
            project={
                "id": self.project.id,
                "state": "ready",
                "name": "game",
                "title": "Game",
                "names_from": "us",
                "versions": ["us"],
                "default_compiler": "fixture",
            },
            workspace={"id": self.project.workspace_id},
            paths={
                "roms": "roms",
                "build": "build",
                "work": "build/work",
                "drafts": "build/drafts",
                "src": "src",
                "include": ["include"],
                "asm": "asm",
                "tools": "tools",
            },
            version={
                "us": {
                    "baserom": "roms/baserom.us.z64",
                    "baserom_sha1": self.project.version("us").baserom_sha1,
                    "split": "versions/us/game.yaml",
                    "symbols": "versions/us/symbol_addrs.txt",
                    "macros": [],
                }
            },
            units={"main": "fixture"},
        )
        data["compilers"]["fixture"]["cflags"] = []
        (self.root / "config.toml").write_text(toml.dumps(data))
        self.project = config.load(self.root)
        setup.run(self.project, self.policy)
        self.project = config.load(self.root)
        self.settings = config.load_policy(stage="setup")
        for name, content in {
            "README.md": b"Owner README\r\n\xff",
            "CONTRIBUTING.md": b"Owner workflow\r\n",
            "include/custom.h": b"struct Owner { int value; };\n",
            "docs/setup/note.txt": b"Owner evidence\n",
        }.items():
            destination = self.root / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(content)
        generation = self.project.build / "us.0"
        generation.mkdir(parents=True)
        (generation / "game.us.z64").write_bytes(b"ABC")
        self.project.build_link("us").symlink_to(generation.name)

    @staticmethod
    def proof(project: config.Project, version: str, data: bytes, cores: int, *, log: Path) -> None:
        generation = project.build / f"{version}.0"
        generation.mkdir(parents=True)
        project.build_link(version).symlink_to(generation.name)
        (generation / f"{project.name}.{version}.z64").write_bytes(data)
        project.asm.mkdir(parents=True)
        (project.asm / "generated.s").write_bytes(b".text\n")
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text("check: OK\n")

    def test_refresh_proves_staged_inputs_preserving_human_files(self) -> None:
        before = setup._inputs(self.project)
        with patch.object(setup_proof, "proof", side_effect=self.proof) as proof:
            lines = setup.refresh(self.project, self.settings)
        self.assertEqual(proof.call_args.args[0].root.parent.parent, self.project.build / "setup")
        self.assertIn(hashlib.sha1(b"ABC").hexdigest(), lines[0])
        self.assertEqual(setup._inputs(self.project), before)
        self.assertEqual(os.readlink(self.project.build_link("us")), "us.1")
        self.assertEqual((self.project.asm / "generated.s").read_bytes(), b".text\n")

    def test_proof_failure_leaves_files_and_generations_unchanged(self) -> None:
        before = setup._inputs(self.project)
        with (
            patch.object(setup_proof, "proof", side_effect=config.Held("setup", "setup.sha1.us: injected failure")),
            self.assertRaisesRegex(config.Held, "setup.sha1.us"),
        ):
            setup.refresh(self.project, self.settings)
        self.assertEqual(setup._inputs(self.project), before)
        self.assertEqual(os.readlink(self.project.build_link("us")), "us.0")
        self.assertFalse(self.project.asm.exists())
        self.assertFalse((self.project.build / "us.1").exists())

    def test_changed_input_during_proof_refuses_publication(self) -> None:
        def proof(*args: object, **kwargs: object) -> None:
            self.proof(*args, **kwargs)  # type: ignore[arg-type]
            (self.project.src / "middle.c").write_bytes(b"Owner edit during proof")

        with (
            patch.object(setup_proof, "proof", side_effect=proof),
            self.assertRaisesRegex(config.Held, "setup.publication: project inputs changed"),
        ):
            setup.refresh(self.project, self.settings)
        self.assertEqual((self.project.src / "middle.c").read_bytes(), b"Owner edit during proof")
        self.assertEqual(os.readlink(self.project.build_link("us")), "us.0")
        self.assertFalse(self.project.asm.exists())

    def test_changed_generation_during_proof_refuses_publication(self) -> None:
        other = self.project.build / "us.8"
        other.mkdir()

        def proof(*args: object, **kwargs: object) -> None:
            self.proof(*args, **kwargs)  # type: ignore[arg-type]
            setup._swap(self.project.build_link("us"), other.name)

        with (
            patch.object(setup_proof, "proof", side_effect=proof),
            self.assertRaisesRegex(config.Held, "setup.publication: current generations changed"),
        ):
            setup.refresh(self.project, self.settings)
        self.assertEqual(os.readlink(self.project.build_link("us")), "us.8")
        self.assertFalse(self.project.asm.exists())

    def test_exception_after_generation_swap_rolls_back_all_writes(self) -> None:
        before = setup._inputs(self.project)
        swap = setup._swap
        calls = 0

        def fail_once(link: Path, target: str) -> None:
            nonlocal calls
            swap(link, target)
            calls += 1
            if calls == 1:
                raise RuntimeError("injected after swap")

        with (
            patch.object(setup_proof, "proof", side_effect=self.proof),
            patch.object(setup, "_swap", side_effect=fail_once),
            self.assertRaisesRegex(RuntimeError, "injected after swap"),
        ):
            setup.refresh(self.project, self.settings)
        self.assertEqual(setup._inputs(self.project), before)
        self.assertEqual(os.readlink(self.project.build_link("us")), "us.0")
        self.assertFalse(self.project.asm.exists())
        self.assertFalse((self.project.build / "us.1").exists())

    def test_failure_after_ready_write_restores_previous_config(self) -> None:
        before = setup._inputs(self.project)
        atomic = setup.compiler_files.atomic_bytes
        failed = False

        def fail_once(path: Path, content: bytes, *, mode: int = 0o644) -> None:
            nonlocal failed
            atomic(path, content, mode=mode)
            if path == self.root / "config.toml" and not failed:
                failed = True
                raise OSError("injected after ready write")

        with tempfile.TemporaryDirectory(dir=self.project.build) as temporary:
            tree = Path(temporary) / "tree"
            setup._copy_inputs(self.project, tree, before)
            staged = config.load(tree)
            self.proof(staged, "us", b"ABC", 1, log=self.project.build / "proof.log")
            with (
                patch.object(setup.compiler_files, "atomic_bytes", side_effect=fail_once),
                self.assertRaisesRegex(OSError, "injected after ready write"),
            ):
                setup._publish(
                    self.project, staged, before, fresh=True, generations=setup._generations(self.project, ("us",))
                )
        self.assertEqual(setup._inputs(self.project), before)
        self.assertEqual(os.readlink(self.project.build_link("us")), "us.0")
        self.assertFalse(self.project.asm.exists())
        self.assertFalse((self.project.build / "us.1").exists())

    def test_byte_mismatch_has_named_refusal_offset_and_sizes(self) -> None:
        built = self.project.build_link("us") / "game.us.z64"
        built.write_bytes(b"AX")
        with (
            patch.object(setup_proof, "run"),
            self.assertRaisesRegex(config.Held, r"setup.sha1.us:.*offset 0x1.*sizes 3/2"),
        ):
            setup_proof.proof(self.project, "us", b"ABC", 1)

    def test_unconfirmed_proposal_does_not_stage_ready_configuration(self) -> None:
        data = tomllib.loads((self.root / "config.toml").read_text())
        data["project"]["state"] = "awaiting-roms"
        del data["project"]["default_compiler"]
        del data["compilers"]
        del data["units"]
        (self.root / "config.toml").write_text(toml.dumps(data))
        pending = config.load_pending(self.root)
        before = setup._inputs(pending)
        with (
            patch.object(fingerprint, "receipt", return_value=[]),
            patch.object(
                fingerprint,
                "confirm_proposal",
                side_effect=config.Held("setup", "setup.compiler_confirmation: explicit acceptance required"),
            ),
            patch.object(setup, "_copy_inputs") as staging,
            self.assertRaisesRegex(config.Held, "setup.compiler_confirmation"),
        ):
            setup.complete_setup(pending, Mock(), Mock(), Mock(), self.settings)
        staging.assert_not_called()
        self.assertEqual(setup._inputs(pending), before)
        self.assertEqual(config.load_pending(self.root).state, "awaiting-roms")
