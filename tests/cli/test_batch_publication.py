"""Installed CLI batches attribute simultaneous byte faults and retain learned results."""

import hashlib
import json
import os
import signal
import subprocess
import sys
import time
import unittest

import toml

from tests.cli import test_publication_boundary as fixture
from unbake.project import config, makefile


class BatchPublicationCliTests(unittest.TestCase):
    names = tuple(f"item_{index:02d}" for index in range(32))
    setUp = fixture.PublicationBoundaryCliTests.setUp
    write_policy = fixture.PublicationBoundaryCliTests.write_policy
    cli = fixture.PublicationBoundaryCliTests.cli
    make = fixture.PublicationBoundaryCliTests.make

    def planter(self, *, block=False):
        from dataclasses import replace

        bad = {self.names[5], self.names[17], self.names[29]}
        wrapper = self.directory / "plant-link"
        marker = self.directory / "subset-link"
        calls = self.directory / "link-calls.jsonl"
        wrapper.write_text(
            f"#!{sys.executable}\n"
            "import json, os, pathlib, sys, time\n"
            "from unbake.project_tools.elf import Object\n"
            f"bad={bad!r}\n"
            "sources=[pathlib.Path(p) for p in sys.argv[1:] if p.startswith('obj/src/') and p.endswith('.o')]\n"
            f"with pathlib.Path({str(calls)!r}).open('a') as output:\n"
            " output.write(json.dumps([p.stem for p in sources])+'\\n')\n"
            "for path in sources:\n"
            " if path.stem in bad:\n"
            "  obj=Object(path); section=obj.section('.text'); at=obj.sections[section][4]+7\n"
            "  obj.data[at]=2; path.write_bytes(obj.data)\n"
            f"if {block!r} and len(sources)<{len(self.names)}:\n"
            f" pathlib.Path({str(marker)!r}).touch()\n"
            " time.sleep(30)\n"
            f"os.execv({str(self.policy.mips_ld)!r}, [{str(self.policy.mips_ld)!r}, *sys.argv[1:]])\n"
        )
        wrapper.chmod(0o755)
        self.policy = replace(self.policy, mips_ld=wrapper)
        self.write_policy()
        return bad, marker

    def evidence(self):
        return [
            json.loads(line)
            for path in (self.root / ".unbake/state/publications").glob("*.jsonl")
            for line in path.read_text().splitlines()
        ]

    def test_second_batch_reuses_published_objects_and_links_once(self):
        self.cli("submit", self.sources[0])
        generations = {v: (self.project.build / v).resolve() for v in self.project.versions}
        retained = {v: (g / "obj/src" / (self.names[0] + ".o")).read_bytes() for v, g in generations.items()}
        # An unrelated new header must not invalidate the previously published unit.
        (self.project.include[0] / "unused.h").write_text("typedef int Unused;\n")
        self.cli("submit", "--batch", *self.sources[1:])
        compiled = [row for row in self.evidence() if row["event"] == "compile"]
        self.assertEqual(len(compiled), 2 * len(self.project.versions))
        later = [row for row in compiled if len(row["sources"]) > 1]
        self.assertEqual(len(later), len(self.project.versions))
        for row in later:
            self.assertEqual(set(row["sources"]), set(self.names[1:]))
        proofs = [row for row in self.evidence() if row["event"] == "proof"]
        self.assertEqual(len(proofs), 2)
        self.assertTrue(all(not row["failures"] for row in proofs))
        for version, content in retained.items():
            generation = (self.project.build / version).resolve()
            self.assertEqual((generation / "obj/src" / (self.names[0] + ".o")).read_bytes(), content)
            self.assertTrue((generation / "obj/asm").is_symlink())
            self.assertTrue((generation / "obj/asm").is_dir())
        self.assertIn(": OK", self.make())

    def test_changed_header_cannot_reuse_a_now_mismatching_published_object(self):
        header = self.project.include[0] / "value.h"
        header.write_text("#define PROOF_VALUE 1\n")
        first = self.sources[0]
        first.write_text(f'#include "value.h"\nint {first.stem}(void) {{ return PROOF_VALUE; }}\n')
        self.cli("submit", first)
        generations = {v: (self.project.build / v).resolve() for v in self.project.versions}
        header.write_text("#define PROOF_VALUE 2\n")
        result = subprocess.run(
            [str(self.script), "--project", str(self.root), "submit", str(self.sources[1])],
            env=self.env,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        output = result.stdout + result.stderr
        self.assertEqual(result.returncode, 1, output)
        self.assertIn("submit.dependencies", output)
        self.assertIn(self.names[0], output)
        self.assertFalse((self.project.src / self.sources[1].name).exists())
        self.assertEqual(generations, {v: (self.project.build / v).resolve() for v in self.project.versions})

    def test_three_changed_sources_are_isolated_and_29_publish(self):
        for source in self.sources:
            self.cli("try", source)
        bad = {self.names[5], self.names[17], self.names[29]}
        for source in self.sources:
            if source.stem in bad:
                source.write_text(source.read_text().replace("return 1", "return 2"))
        result = subprocess.run(
            [str(self.script), "--project", str(self.root), "submit", "--batch", *map(str, self.sources)],
            env=self.env,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        for name in self.names:
            if name in bad:
                # A source edited after its try is proved like any other and refused by its bytes.
                self.assertIn(f"HELD(match): {name}: build compare failed", result.stdout)
                self.assertFalse((self.project.src / (name + ".c")).exists())
            else:
                self.assertIn(f"{name} matched on VERSION", result.stdout)
        proofs = [row for row in self.evidence() if row["event"] == "proof"]
        # Object preflight names byte faults before the passing subset links once.
        self.assertEqual(len(proofs), 1)
        self.assertEqual(len(proofs[-1]["sources"]), 29)
        self.assertFalse(proofs[-1]["failures"])
        self.assertIn(": OK", self.make())

    def test_three_bad_objects_in_32_publish_29_in_two_proofs(self):
        # Projects retain generated helpers from setup. A submit must stage
        # current drivers and their checksums before its cartridge proof.
        checksum = self.project.tools / "compiler.sha256"
        checksum_text = checksum.read_text()
        for filename in ("compile.py", "pool_slices.py"):
            helper = self.project.tools / filename
            old_digest = hashlib.sha256(helper.read_bytes()).hexdigest()
            helper.write_text("raise RuntimeError('obsolete generated helper')\n" + helper.read_text())
            new_digest = hashlib.sha256(helper.read_bytes()).hexdigest()
            checksum_text = checksum_text.replace(old_digest, new_digest)
        checksum.write_text(checksum_text)
        for source in self.sources:
            self.cli("try", source)
        bad, _ = self.planter()
        result = subprocess.run(
            [str(self.script), "--project", str(self.root), "submit", "--batch", *map(str, self.sources)],
            env=self.env,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        for name in self.names:
            if name in bad:
                self.assertIn(f"HELD(match): {name}: build compare failed", result.stdout)
                self.assertIn(f"object obj/src/{name}.o .text", result.stdout)
                self.assertIn(f"symbol {name}", result.stdout)
                self.assertFalse((self.project.src / (name + ".c")).exists())
            else:
                self.assertIn(f"{name} matched on VERSION", result.stdout)
                self.assertTrue((self.project.src / (name + ".c")).exists())
        proofs = [row for row in self.evidence() if row["event"] == "proof"]
        self.assertEqual(len(proofs), 2, proofs)
        self.assertEqual(len(proofs[0]["sources"]), 32)
        self.assertEqual(len(proofs[1]["sources"]), 29)
        self.assertFalse(proofs[1]["failures"])
        expected_helpers = makefile.helpers(config.load(self.root))
        for filename in ("compile.py", "pool_slices.py"):
            helper = self.project.tools / filename
            self.assertEqual(helper.read_text(), expected_helpers["tools/" + filename])
            self.assertIn(hashlib.sha256(helper.read_bytes()).hexdigest(), checksum.read_text())
        self.assertNotIn("compiler_selections", toml.loads((self.root / "config.toml").read_text()))
        self.assertIn(": OK", self.make())

    def test_stopped_subset_proof_keeps_named_refusals_on_disk_and_stdout(self):
        for source in self.sources:
            self.cli("try", source)
        bad, marker = self.planter(block=True)
        output = self.directory / "stopped.out"
        with output.open("w") as stream:
            process = subprocess.Popen(
                [str(self.script), "--project", str(self.root), "submit", "--batch", *map(str, self.sources)],
                env=self.env,
                stdout=stream,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            try:
                deadline = time.monotonic() + 90
                while not marker.exists() and process.poll() is None and time.monotonic() < deadline:
                    time.sleep(0.05)
                self.assertTrue(marker.exists(), output.read_text())
                refusals = [row["line"] for row in self.evidence() if row["event"] == "receipt"]
                for name in bad:
                    self.assertTrue(any(f"HELD(match): {name}:" in line for line in refusals))
                    self.assertIn(f"HELD(match): {name}:", output.read_text())
            finally:
                if process.poll() is None:
                    os.killpg(process.pid, signal.SIGTERM)
                process.wait(timeout=10)
        self.assertEqual(len([row for row in self.evidence() if row["event"] == "proof"]), 1)
        self.assertFalse(list(self.project.src.glob("*.c")))
