"""Setup proves isolated inputs and rolls back every publication mutation."""

import hashlib
import os
import shutil
import subprocess
import sys
import sysconfig
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import toml

from tests.project.makefile_fixture import WORK, fixture
from tests.project.test_bootstrap import cartridge
from tests.project.test_config import write_policy
from unbake.project import config, fingerprint, init, setup, setup_proof
from unbake.project.census import Census
from unbake.report import readme_layout


class SetupTransactionTests(unittest.TestCase):
    def setUp(self) -> None:
        WORK.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=WORK)
        self.addCleanup(self.temporary.cleanup)
        self.addCleanup(patch.stopall)
        patch.object(setup_proof, "extract").start()
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
            units={},
        )
        data["compilers"]["fixture"]["cflags"] = []
        (self.root / "config.toml").write_text(toml.dumps(data))
        self.project = config.load(self.root)
        setup.run(self.project, self.policy)
        self.project = config.load(self.root)
        (self.project.include[0] / "types.h").write_text("typedef unsigned int u32;\ntypedef unsigned long long u64;\n")
        setup._sdk_headers(self.project)
        policy_path = write_policy(self.root)
        patch.dict(os.environ, UNBAKE_POLICY=str(policy_path)).start()
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
    def proof(
        project: config.Project,
        version: str,
        data: bytes | Path,
        cores: int,
        *,
        log: Path,
        slots: setup_proof.JobSlots | None = None,
        extracted: bool = False,
    ) -> None:
        generation = project.build / f"{version}.0"
        generation.mkdir(parents=True)
        project.build_link(version).symlink_to(generation.name)
        (generation / f"{project.name}.{version}.z64").write_bytes(
            data.read_bytes() if isinstance(data, Path) else data
        )
        project.asm.mkdir(parents=True, exist_ok=True)
        (project.asm / "generated.s").write_bytes(b".text\n")
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text("check: OK\n")

    def test_refresh_proves_staged_inputs_preserving_human_files(self) -> None:
        before = setup._inputs(self.project)
        with patch.object(setup_proof, "proof", side_effect=self.proof) as proof:
            lines = setup.refresh(self.project, self.settings)
        self.assertEqual(proof.call_args.args[0].root.parent.parent, self.project.build / "setup")
        self.assertIn(hashlib.sha1(b"ABC").hexdigest(), lines[0])
        # Refresh rewrites config.toml in canonical form; every other input is preserved.
        after = setup._inputs(self.project)
        changed = {k for k in after if after[k] != before.get(k)} | (set(before) - set(after))
        self.assertLessEqual(changed, {"config.toml"})
        self.assertEqual(os.readlink(self.project.build_link("us")), "us.1")
        self.assertEqual((self.project.asm / "generated.s").read_bytes(), b".text\n")

    def test_installed_refresh_restores_owned_callbacks_after_proving_template(self) -> None:
        callback = self.project.include[0] / "shared/audio_callbacks.h"
        template = (setup.makefile.TEMPLATES / "audio_callbacks.h").read_bytes()
        repaired = template.replace(
            b"void *, short *, int, int, Acmd *", b"void *driver, short *samples, int count, int stride, Acmd *commands"
        )
        callback.write_bytes(repaired)
        # Fixture executables trace their calls. Put that trace in build so
        # the real concurrent-input guard does not mistake it for an edit.
        from unbake.project import toolchain

        registry = toolchain.REGISTRY_PATH
        manifest = self.project.tools / "compiler.sha256"
        for path in self.project.tools.rglob("*"):
            if not path.is_file() or not path.read_bytes().startswith(b"#!"):
                continue
            before = path.read_bytes()
            changed = before.replace(str(self.root / "calls").encode(), str(self.project.build / "calls").encode())
            if changed == before:
                continue
            path.write_bytes(changed)
            old, new = hashlib.sha256(before).hexdigest(), hashlib.sha256(changed).hexdigest()
            for pins in (registry, manifest):
                pins.write_text(pins.read_text().replace(old, new))
        for name in ("cc", "as"):
            shutil.copy2(self.project.tools / "fixture" / name, self.root / "cache/compilers/fixture" / name)
        cache = self.project.build / "cache"
        shutil.copytree(self.root / "cache", cache)
        local_policy = self.project.build / "installed-policy.toml"
        policy_file = config.policy_path(None)
        local_policy.write_text(policy_file.read_text().replace(str(self.root / "cache"), str(cache)))
        inputs = setup._inputs(self.project)
        script = Path(sysconfig.get_path("scripts")) / "unbake"
        launcher = self.root / "installed-setup.py"
        # Substitute only the compiler registry with the fixture's byte-pinned
        # tools. Staging, make extraction/check and publication run normally.
        launcher.write_text(
            "import runpy, sys\nfrom pathlib import Path\nfrom unbake.project import toolchain\n"
            f"toolchain.REGISTRY_PATH = Path({str(toolchain.REGISTRY_PATH)!r})\n"
            f"sys.argv[0] = {str(script)!r}\nrunpy.run_path({str(script)!r}, run_name='__main__')\n"
        )
        environment = dict(os.environ, PYTHONNOUSERSITE="1")
        environment.pop("PYTHONPATH", None)
        result = subprocess.run(
            [sys.executable, str(launcher), "--project", str(self.root), "--policy", str(local_policy), "setup"],
            env=environment,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(callback.read_bytes(), template)
        self.assertEqual((self.project.build_link("us") / "game.us.z64").read_bytes(), b"ABC")
        after = setup._inputs(self.project)
        for name in ("README.md", "CONTRIBUTING.md", "include/custom.h", "src/middle.c"):
            self.assertEqual(after[name], inputs[name])

    def test_failed_refresh_preserves_repaired_callbacks(self) -> None:
        callback = self.project.include[0] / "shared/audio_callbacks.h"
        repaired = callback.read_bytes().replace(b"void *, int, void *", b"void *driver, int param, void *value")
        callback.write_bytes(repaired)

        def refuse(project: config.Project, *args: object, **kwargs: object) -> None:
            self.assertEqual(
                (project.include[0] / "shared/audio_callbacks.h").read_bytes(),
                (setup.makefile.TEMPLATES / "audio_callbacks.h").read_bytes(),
            )
            raise config.Held("setup", "setup.sha1.us: injected failure")

        with (
            patch.object(setup_proof, "proof", side_effect=refuse),
            self.assertRaisesRegex(config.Held, "setup.sha1.us"),
        ):
            setup.refresh(self.project, self.settings)
        self.assertEqual(callback.read_bytes(), repaired)

    def test_publication_preserves_proved_assembly_mtime(self) -> None:
        timestamp = 1_600_000_000_123_456_789

        def proof(*args: object, **kwargs: object) -> None:
            self.proof(*args, **kwargs)  # type: ignore[arg-type]
            project = args[0]
            assert isinstance(project, config.Project)
            os.utime(project.asm / "generated.s", ns=(timestamp, timestamp))

        with patch.object(setup_proof, "proof", side_effect=proof):
            setup.refresh(self.project, self.settings)
        self.assertEqual((self.project.asm / "generated.s").stat().st_mtime_ns, timestamp)

    def test_proof_failure_leaves_files_and_generations_unchanged(self) -> None:
        before = setup._inputs(self.project)
        with (
            patch.object(setup_proof, "proof", side_effect=config.Held("setup", "setup.sha1.us: injected failure")),
            self.assertRaisesRegex(config.Held, "setup.sha1.us"),
        ):
            setup.refresh(self.project, self.settings)
        # Refresh rewrites config.toml in canonical form; every other input is preserved.
        after = setup._inputs(self.project)
        changed = {k for k in after if after[k] != before.get(k)} | (set(before) - set(after))
        self.assertLessEqual(changed, {"config.toml"})
        self.assertEqual(os.readlink(self.project.build_link("us")), "us.0")
        self.assertFalse(self.project.asm.exists())
        self.assertFalse((self.project.build / "us.1").exists())

    def test_extraction_failure_never_builds_or_publishes(self) -> None:
        before = setup._inputs(self.project)
        with (
            patch.object(setup_proof, "extract", side_effect=config.Held("setup", "setup.sha1.us: extraction failed")),
            patch.object(setup_proof, "proof") as proof,
            self.assertRaisesRegex(config.Held, "setup.sha1.us"),
        ):
            setup.refresh(self.project, self.settings)
        proof.assert_not_called()
        # Refresh rewrites config.toml in canonical form; every other input is preserved.
        after = setup._inputs(self.project)
        changed = {k for k in after if after[k] != before.get(k)} | (set(before) - set(after))
        self.assertLessEqual(changed, {"config.toml"})
        self.assertEqual(os.readlink(self.project.build_link("us")), "us.0")

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
        # Refresh rewrites config.toml in canonical form; every other input is preserved.
        after = setup._inputs(self.project)
        changed = {k for k in after if after[k] != before.get(k)} | (set(before) - set(after))
        self.assertLessEqual(changed, {"config.toml"})
        self.assertEqual(os.readlink(self.project.build_link("us")), "us.0")
        self.assertFalse(self.project.asm.exists())
        self.assertFalse((self.project.build / "us.1").exists())

    def test_failure_after_ready_write_restores_previous_config(self) -> None:
        before = setup._inputs(self.project)
        original_mtime = (self.root / "config.toml").stat().st_mtime_ns
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
        # Refresh rewrites config.toml in canonical form; every other input is preserved.
        after = setup._inputs(self.project)
        changed = {k for k in after if after[k] != before.get(k)} | (set(before) - set(after))
        self.assertLessEqual(changed, {"config.toml"})
        self.assertEqual((self.root / "config.toml").stat().st_mtime_ns, original_mtime)
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

    def test_streamed_cartridge_mismatch_crosses_chunk_boundary(self) -> None:
        expected = self.root / "expected.z64"
        expected.write_bytes(bytes(1024 * 1024) + b"ABCD")
        built = self.project.build_link("us") / "game.us.z64"
        built.write_bytes(bytes(1024 * 1024) + b"AXCD")
        with (
            patch.object(setup_proof, "run"),
            self.assertRaisesRegex(config.Held, r"setup.sha1.us:.*offset 0x100001.*expected 424344.*produced 584344"),
        ):
            setup_proof.proof(self.project, "us", expected, 1)

    def test_unconfirmed_proposal_does_not_stage_ready_configuration(self) -> None:
        data = tomllib.loads((self.root / "config.toml").read_text())
        data["project"]["state"] = "awaiting-roms"
        del data["project"]["default_compiler"]
        del data["compilers"]
        data.pop("units", None)
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

    def test_fresh_completion_publishes_only_after_proof_and_revalidates_proposal(self) -> None:
        data = tomllib.loads((self.root / "config.toml").read_text())
        data["project"]["state"] = "awaiting-roms"
        del data["project"]["default_compiler"]
        del data["compilers"]
        data.pop("units", None)
        (self.root / "config.toml").write_text(toml.dumps(data))
        pending = config.load_pending(self.root)
        rom = cartridge(self.project.version("us").baserom, b"ABC")
        census = Census((rom,), {rom.path: "us"}, "us", {}, {}, self.project.build / "setup/roms.json")
        layout = {
            "schema": 1,
            "project_id": pending.id,
            "workspace_id": pending.workspace_id,
            "rom_sha1": {"us": rom.sha1},
            "names_from": "us",
            "inputs_sha256": {},
            "versions": {
                "us": {
                    "functions": [],
                    "providers": [],
                    "loaded_spans": [],
                    "evidence": {
                        "split_yaml": self.project.version("us").split.read_text(),
                        "symbols_text": self.project.version("us").symbols.read_text(),
                        "resident_mappings": [],
                    },
                }
            },
        }
        proposal = {
            "schema": 1,
            "project_id": pending.id,
            "workspace_id": pending.workspace_id,
            "rom_sha1": {"us": rom.sha1},
            "layout_sha256": "a" * 64,
            "inputs_sha256": {},
            "default_compiler": "fixture",
            "assignments": {"main": "fixture"},
            "cflags": {"fixture": []},
            "candidates": {},
            "unresolved": [],
        }
        proposal_path = self.project.build / "setup/proposal.json"
        proposal_path.parent.mkdir(parents=True)
        accepted = b'{"reviewed": true}\n'
        proposal_path.write_bytes(accepted)
        token = hashlib.sha256(accepted).hexdigest()

        def prove(*args: object, **kwargs: object) -> None:
            self.assertEqual(config.load_pending(self.root).state, "awaiting-roms")
            self.assertNotIn("compilers", tomllib.loads((self.root / "config.toml").read_text()))
            self.proof(*args, **kwargs)  # type: ignore[arg-type]

        with (
            patch.object(fingerprint, "receipt", return_value=[]),
            patch.object(fingerprint, "confirm_proposal") as confirmation,
            patch("unbake.project.compiler_proposal.confirmation_guard", return_value=Mock()) as guard,
            patch.object(setup_proof, "proof", side_effect=prove),
        ):
            setup.complete_setup(pending, census, layout, proposal, self.settings, confirm=token)
        self.assertEqual(config.load_pending(self.root).state, "ready")
        self.assertEqual(confirmation.call_count, 1)
        guard.return_value.assert_called_once_with()
        self.assertEqual(confirmation.call_args.kwargs["confirm"], token)
        self.assertEqual((self.root / "build/setup/compiler.json").read_bytes(), accepted)
        self.assertEqual((self.root / "versions/us/baserom.sha1").read_text(), rom.sha1 + "  roms/baserom.us.z64\n")
        self.assertEqual(os.readlink(self.project.build_link("us")), "us.1")

    def test_ready_progress_readme_replaces_only_untouched_shell(self) -> None:
        rom = cartridge(self.project.version("us").baserom, b"ABC")
        census = Census((rom,), {rom.path: "us"}, "us", {}, {}, self.project.build / "setup/roms.json")
        layout = {"versions": {"us": {"functions": [{"start": 0, "end": 3}]}}}
        tree = self.project.build / "readme-stage"
        tree.mkdir()
        readme = self.root / "README.md"
        owner = readme.read_bytes()
        setup._ready_readme(self.project, census, layout, tree)
        self.assertFalse((tree / "README.md").exists())
        self.assertEqual(readme.read_bytes(), owner)
        readme.write_text(init.readme_text(self.root))
        setup._ready_readme(self.project, census, layout, tree)
        generated = (tree / "README.md").read_text()
        _before, block, _after = readme_layout.section(generated)
        self.assertTrue(readme_layout.complete(block))
        self.assertNotIn("@", generated)
        self.assertIn("0 of 3", generated)
        self.assertEqual(readme.read_text(), init.readme_text(self.root))

    def test_seeded_generation_and_assembly_are_isolated_from_live_outputs(self) -> None:
        current = self.project.build_link("us").resolve()
        dependency = current / ".split.mk"
        original = f"target: {self.project.root}/asm/us/example.s\n"
        dependency.write_text(original)
        (current / ".extract-key").write_text("content-key")
        self.project.asm.mkdir()
        (self.project.asm / "example.s").write_bytes(b"original assembly")
        before = setup._inputs(self.project)
        tree = self.project.build / "seed-stage"
        setup._copy_inputs(self.project, tree, before)
        staged = config.load(tree)
        setup._seed_generations(self.project, staged)
        copied = staged.build_link("us") / ".split.mk"
        self.assertIn(str(tree), copied.read_text())
        self.assertEqual(dependency.read_text(), original)
        copied.write_text("stage-only edit")
        (staged.asm / "example.s").write_bytes(b"stage-only assembly edit")
        self.assertEqual(dependency.read_text(), original)
        self.assertEqual((self.project.asm / "example.s").read_bytes(), b"original assembly")
        self.assertEqual(os.readlink(self.project.build_link("us")), "us.0")

    def test_duplicate_gfx_definition_refuses_without_overwriting_human_header(self) -> None:
        header = self.project.include[0] / "human.h"
        content = "/* Owner header */\ntypedef union { unsigned int words[2]; } Gfx;\n"
        header.write_text(content)
        with self.assertRaisesRegex(config.Held, r"setup.gfx_type: include/human.h:2"):
            setup._sdk_headers(self.project)
        self.assertEqual(header.read_text(), content)

    def test_open_gbi_and_one_shared_sdk_type_are_installed(self) -> None:
        root = self.project.include[0]
        self.assertEqual((root / "gbi.h").read_bytes(), (setup.makefile.TEMPLATES / "gbi.h").read_bytes())
        self.assertIn("MIT License", (root / "gbi.h").read_text())
        self.assertIn('include "shared/gfx.h"', (root / "n64sdk.h").read_text())
        from unbake.decomp.gbi_source import gfx_typedefs

        declarations = [path for path in root.rglob("*.h") if gfx_typedefs(path.read_text())]
        self.assertEqual(declarations, [root / "shared/gfx.h"])
        before = {path: path.stat().st_mtime_ns for path in root.rglob("*.h")}
        setup._sdk_headers(self.project)
        self.assertEqual({path: path.stat().st_mtime_ns for path in before}, before)

    def test_one_version_failure_prevents_all_version_publication(self) -> None:
        data = tomllib.loads((self.root / "config.toml").read_text())
        data["project"]["versions"] = ["us", "eu"]
        data["version"]["eu"] = dict(data["version"]["us"])
        data["version"]["eu"]["baserom"] = "roms/baserom.eu.z64"
        (self.root / "roms/baserom.eu.z64").write_bytes(b"ABC")
        (self.root / "config.toml").write_text(toml.dumps(data))
        project = config.load(self.root)
        before = setup._inputs(project)
        completed = []

        def proof(
            selected: config.Project,
            version: str,
            raw: bytes,
            cores: int,
            *,
            log: Path,
            slots: setup_proof.JobSlots | None = None,
            extracted: bool = False,
        ) -> None:
            if version == "eu":
                raise config.Held("setup", "setup.sha1.eu: injected second-version failure")
            self.proof(selected, version, raw, cores, log=log)
            completed.append(version)

        with (
            patch.object(setup_proof, "proof", side_effect=proof),
            self.assertRaisesRegex(config.Held, "setup.sha1.eu"),
        ):
            setup.refresh(project, self.settings)
        self.assertEqual(completed, ["us"])
        self.assertEqual(setup._inputs(project), before)
        self.assertEqual(os.readlink(project.build_link("us")), "us.0")
        self.assertFalse(project.build_link("eu").exists())
        self.assertFalse(project.asm.exists())
