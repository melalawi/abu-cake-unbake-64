"""Installed publication proofs with real compiler candidates and overlapping processes."""

import fcntl
import hashlib
import json
import os
import shutil
import subprocess
import sys
import sysconfig
import tempfile
import time
import unittest
from dataclasses import asdict, replace
from pathlib import Path

import toml

from tests.support import test_policy
from unbake.project import config, setup
from unbake.report import progress


class PublicationBoundaryCliTests(unittest.TestCase):
    def setUp(self):
        names = getattr(self, "names", ("alpha", "beta"))
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.root = self.directory / "project"
        shutil.copytree(Path(__file__).parents[1] / "fixture", self.root)
        shutil.rmtree(self.root / "src")
        (self.root / "src").mkdir()
        data = toml.loads((self.root / "config.toml").read_text())
        data["project"].update(default_compiler="ido-7.1")
        data["build"]["asflags"] = ["-EB", "-mips3", "-G0", "--no-pad-sections"]
        for key, field in (
            ("as", "mips_as"),
            ("ld", "mips_ld"),
            ("objcopy", "mips_objcopy"),
            ("splat", "splat"),
            ("cpp", "cpp"),
        ):
            data["build"][key] = "policy:" + field
        data["compilers"]["ido-5.3"] = dict(data["compilers"]["ido-7.1"])
        data["compiler_ties"] = {"tie:unit:" + name: ["ido-5.3", "ido-7.1"] for name in names}
        data["units"] = {name: "tie:unit:" + name for name in names}
        body = bytes.fromhex("03e0000824020001") * len(names)
        image = bytes.fromhex("80371240") + bytes(60) + body
        for version in data["project"]["versions"]:
            cartridge = data["version"][version]
            path = self.root / cartridge["baserom"]
            path.parent.mkdir(exist_ok=True)
            path.write_bytes(image)
            cartridge["baserom_sha1"] = hashlib.sha1(image).hexdigest()
            path = self.root / cartridge["split"]
            text = path.read_text().split("    subsegments:", 1)[0]
            text = text.replace("[0x0, header, header]", "[0x0, bin, header]")
            text += (
                "    subsegments:\n"
                + "".join(f"      - [0x{0x40 + index * 8:X}, asm, {name}]\n" for index, name in enumerate(names))
                + f"  - [0x{0x40 + len(names) * 8:X}]\n"
            )
            path.write_text(text)
            (self.root / cartridge["symbols"]).write_text(
                "".join(f"{name} = 0x{0x80001000 + index * 8:08X};\n" for index, name in enumerate(names))
            )
        (self.root / "config.toml").write_text(toml.dumps(data))
        self.project = config.load(self.root)
        reports = {
            version: {
                "version": 2,
                "measures": {
                    "complete_code": 0,
                    "total_code": 8 * len(names),
                    "complete_units": 0,
                    "total_units": len(names),
                },
            }
            for version in self.project.versions
        }
        document = (
            "# Fixture\n\n## Progress\n\n"
            + progress.progress(reports, {version: version + " (synthetic)" for version in self.project.versions})
            + "\n\n## Building\n"
        )
        (self.root / "README.md").write_text(document)
        base = test_policy()
        self.policy = replace(base, cores=2, state_root=self.directory / "state")
        setup.run(self.project, self.policy)
        self.project = config.load(self.root)
        self.sources = []
        for name in names:
            source = self.project.drafts / name / (name + ".c")
            source.parent.mkdir(parents=True)
            source.write_text(f"int {name}(void) {{ return 1; }}\n")
            self.sources.append(source)
        self.manifest = self.root / "unbake-exclusions.json"
        self.manifest.write_text(json.dumps({"schema": 1, "functions": list(names)}) + "\n")
        self.script = Path(sysconfig.get_path("scripts")) / "unbake"
        self.env = dict(os.environ, PYTHONNOUSERSITE="1")
        self.env.pop("PYTHONPATH", None)
        self.write_policy()
        subprocess.run(["git", "init", str(self.root)], capture_output=True, check=True)
        self.make()
        self.cli("map")
        self.cli("solve")

    def write_policy(self):
        path = self.directory / "policy.toml"
        values = {key: str(value) if isinstance(value, Path) else value for key, value in asdict(self.policy).items()}
        path.write_text(toml.dumps(values))
        self.env["UNBAKE_POLICY"] = str(path)

    def cli(self, *arguments):
        result = subprocess.run(
            [str(self.script), "--project", str(self.root), *map(str, arguments)],
            env=self.env,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        output = result.stdout + result.stderr
        self.assertEqual(result.returncode, 0, output)
        self.assertNotIn("Traceback", output)
        return output

    def make(self, expected=0):
        result = subprocess.run(
            ["make", "-j4", "check"],
            cwd=self.root,
            env=self.env,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
        return result.stdout + result.stderr

    def inputs(self):
        return {
            path.relative_to(self.root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for relative in ("config.toml", "Makefile", "tools", "include", "versions", "unbake-exclusions.json")
            for path in (
                [self.root / relative] if (self.root / relative).is_file() else (self.root / relative).rglob("*")
            )
            if path.is_file() and "__pycache__" not in path.parts
        }

    def install_sdk(self):
        (self.project.include[0] / "types.h").write_text(
            "#ifndef TYPES_H\n#define TYPES_H\ntypedef unsigned int u32; typedef unsigned long long u64;\n#endif\n"
        )
        setup._sdk_headers(self.project)

    def test_installed_submit_resolves_sdk_commands_and_callbacks_and_folds_shared_types(self):
        self.install_sdk()
        source = self.sources[0]
        source.write_text(
            '#include "shared/audio_callbacks.h"\n'
            "struct Holder { Acmd command; ALCmdHandler callback; };\n"
            "int alpha(void) { return 1; }\n"
        )
        before = self.inputs()
        self.assertIn("identical 2 of 2", self.cli("try", source))
        self.assertEqual(self.inputs(), before)
        output = self.cli("submit", source)
        self.assertIn("alpha matched on VERSION us, us-rev1", output)
        header = self.project.include[0] / "shared/alpha.h"
        self.assertIn("Acmd command;", header.read_text())
        self.assertIn('#include "shared/acmd.h"', header.read_text())
        self.assertIn('#include "shared/audio_callbacks.h"', header.read_text())
        for name in ("gbi.h", "shared/acmd.h", "shared/audio_callbacks.h"):
            path = self.project.include[0] / name
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), before["include/" + name])
        self.assertIn(": OK", self.make())

    def test_installed_batch_shares_one_new_sdk_header_overlay(self):
        from unbake.decomp import work

        self.install_sdk()
        for source in self.sources:
            work.overlay(self.project, source.parent)
            header = source.parent / "overlay/include/shared/holder.h"
            header.write_text(
                '#ifndef HOLDER_H\n#define HOLDER_H\n#include "acmd.h"\n'
                "typedef struct { Acmd command; } Holder;\n#endif\n"
            )
            source.write_text(f'#include "shared/holder.h"\nint {source.stem}(void) {{ return 1; }}\n')
            self.cli("try", source)
        output = self.cli("submit", "--batch", *self.sources)
        for source in self.sources:
            self.assertIn(source.stem + " matched on VERSION us, us-rev1", output)
        self.assertTrue((self.project.include[0] / "shared/holder.h").is_file())
        self.assertIn(": OK", self.make())

    def test_installed_try_reuses_a_header_already_published_from_another_overlay(self):
        from unbake.decomp import work

        self.install_sdk()
        for source in self.sources:
            work.overlay(self.project, source.parent)
            (source.parent / "overlay/include/shared/holder.h").write_text(
                '#ifndef HOLDER_H\n#define HOLDER_H\n#include "acmd.h"\n'
                "typedef struct { Acmd command; } Holder;\n#endif\n"
            )
            source.write_text(f'#include "shared/holder.h"\nint {source.stem}(void) {{ return 1; }}\n')
        self.cli("try", self.sources[0])
        self.cli("submit", self.sources[0])
        self.cli("try", self.sources[1])
        self.cli("submit", self.sources[1])
        self.assertIn(": OK", self.make())

    def test_installed_submit_names_missing_sdk_header_prerequisite_for_exact_source(self):
        self.install_sdk()
        path = self.project.include[0] / "shared/missing_sdk.h"
        path.write_text("typedef struct { MissingSDK words; } MissingRecord;\n")
        source = self.sources[0]
        self.assertIn("identical 2 of 2", self.cli("try", source))
        before = self.inputs()
        for operands in (("submit", source), ("submit", "--batch", source)):
            result = subprocess.run(
                [str(self.script), "--project", str(self.root), *map(str, operands)],
                env=self.env,
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )
            output = result.stdout + result.stderr
            self.assertEqual(result.returncode, 1, output)
            self.assertIn("headers.declaration: SDK/shared header prerequisite", output)
            self.assertIn("MissingSDK: missing type layout", output)
            self.assertIn("include/shared/missing_sdk.h:1", output)
            self.assertIn("Next: Repair the SDK/shared header prerequisite", output)
            self.assertNotIn("Supply alpha", output)
            self.assertEqual(self.inputs(), before)
            self.assertFalse((self.project.src / source.name).exists())

    def test_equivalent_batch_is_read_only_until_one_proved_publication(self):
        before = self.inputs()
        for source in self.sources:
            output = self.cli("try", source)
            self.assertIn("compiler equivalent " + source.stem, output)
            self.assertEqual(self.inputs(), before)
        output = self.cli("submit", "--batch", *self.sources)
        self.assertIn("alpha matched", output)
        self.assertIn("beta matched", output)
        data = toml.loads((self.root / "config.toml").read_text())
        for source in self.sources:
            selection = data["compiler_selections"]["tie:unit:" + source.stem]
            self.assertEqual(selection["status"], "equivalent")
            self.assertEqual(selection["candidates"], ["ido-5.3", "ido-7.1"])
            evidence = json.loads(selection["evidence_json"])
            self.assertNotIn(str(self.directory), selection["evidence_json"])
            for candidate in evidence["candidates"].values():
                for version in candidate["versions"].values():
                    self.assertEqual(version["target_words"], version["candidate_words"])
            self.assertTrue((self.project.src / source.name).is_file())
        self.assertEqual(json.loads(self.manifest.read_bytes())["functions"], [])
        self.assertIn(": OK", self.make())
        subprocess.run(["git", "-C", str(self.root), "add", "."], capture_output=True, check=True)
        self.assertIn("OK(check): no entries", self.cli("check", "--hygiene"))
        # The same cartridge remains exact under either measured compiler member.
        for member in ("ido-5.3", "ido-7.1"):
            for source in self.sources:
                data["units"][source.stem] = member
            (self.root / "config.toml").write_text(toml.dumps(data))
            setup.run(config.load(self.root), self.policy)
            self.assertIn(": OK", self.make())

    def test_failed_cartridge_proof_preserves_compiler_config_and_exclusions(self):
        for source in self.sources:
            self.cli("try", source)
        before = self.inputs()
        broken = self.directory / "refuse-link"
        broken.write_text("#!/bin/sh\nexit 1\n")
        broken.chmod(0o755)
        original = self.policy
        self.policy = replace(self.policy, mips_ld=broken)
        self.write_policy()
        result = subprocess.run(
            [str(self.script), "--project", str(self.root), "submit", "--batch", *map(str, self.sources)],
            env=self.env,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("submit.sha1", result.stdout)
        self.assertEqual(self.inputs(), before)
        self.assertFalse(list(self.project.src.glob("*.c")))
        self.policy = original
        self.write_policy()
        self.cli("submit", "--batch", *self.sources)
        self.assertEqual(json.loads(self.manifest.read_bytes())["functions"], [])

    def wrapper(self, name, executable, condition, marker, release):
        path = self.directory / name
        path.write_text(
            f"#!{sys.executable}\nimport os, pathlib, sys, time\n"
            f"if {condition}:\n"
            f" pathlib.Path({str(marker)!r}).touch()\n"
            " deadline=time.monotonic()+30\n"
            f" while not pathlib.Path({str(release)!r}).exists():\n"
            "  if time.monotonic()>deadline: sys.exit(99)\n"
            "  time.sleep(0.02)\n"
            f"os.execv({str(executable)!r}, [{str(executable)!r}, *sys.argv[1:]])\n"
        )
        path.chmod(0o755)
        return path

    def wait_for(self, path, process):
        deadline = time.monotonic() + 30
        while not path.exists() and process.poll() is None and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertTrue(path.exists(), f"process {process.pid} exited {process.poll()} before {path.name}")

    def test_try_overlaps_submit_proof_and_both_sources_publish(self):
        self.overlap(finishes_during_proof=True)

    def test_submit_publishes_between_another_sources_candidate_measurements(self):
        self.overlap(finishes_during_proof=False)

    def overlap(self, *, finishes_during_proof):
        self.cli("try", self.sources[0])
        trying, tried = self.directory / "trying", self.directory / "tried"
        proving, proved = self.directory / "proving", self.directory / "proved"
        diff = self.wrapper("diff", self.policy.objdiff_cli, "'beta' in sys.argv[1:]", trying, tried)
        ld = self.wrapper("link", self.policy.mips_ld, "os.environ.get('BLOCK_PROOF') == '1'", proving, proved)
        self.policy = replace(
            self.policy, objdiff_cli=diff, objdiff_sha256=hashlib.sha256(diff.read_bytes()).hexdigest(), mips_ld=ld
        )
        self.write_policy()
        before = (self.root / "config.toml").read_bytes()
        processes = []
        try:
            beta = subprocess.Popen(
                [str(self.script), "--project", str(self.root), "try", str(self.sources[1])],
                env=self.env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            processes.append(beta)
            self.wait_for(trying, beta)
            alpha = subprocess.Popen(
                [str(self.script), "--project", str(self.root), "submit", str(self.sources[0])],
                env=dict(self.env, BLOCK_PROOF="1"),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            processes.append(alpha)
            self.wait_for(proving, alpha)
            with (self.project.build / ".lock").open("a+b") as lock, self.assertRaises(BlockingIOError):
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertEqual((self.root / "config.toml").read_bytes(), before)
            if finishes_during_proof:
                tried.touch()
                out, err = beta.communicate(timeout=30)
                self.assertEqual(beta.returncode, 0, out + err)
                self.assertIsNone(alpha.poll())
            proved.touch()
            out, err = alpha.communicate(timeout=30)
            self.assertEqual(alpha.returncode, 0, out + err)
            self.assertIn("alpha matched", out)
            if not finishes_during_proof:
                self.assertIsNone(beta.poll())
                tried.touch()
                out, err = beta.communicate(timeout=30)
                self.assertEqual(beta.returncode, 0, out + err)
            # Publishing alpha has not invalidated beta's already retained receipt.
            output = self.cli("submit", self.sources[1])
            self.assertIn("beta matched", output)
            self.assertEqual(json.loads(self.manifest.read_bytes())["functions"], [])
            self.assertIn(": OK", self.make())
        finally:
            tried.touch()
            proved.touch()
            for process in processes:
                if process.poll() is None:
                    process.terminate()
                process.communicate(timeout=10)
