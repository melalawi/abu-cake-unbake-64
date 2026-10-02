"""Installed setup bounds resident cartridge work as the ROM count grows."""

import hashlib
import json
import os
import signal
import struct
import subprocess
import sys
import sysconfig
import tempfile
import time
import tomllib
import unittest
from pathlib import Path

from tests.project.test_rom import cartridge


class SetupMemoryTests(unittest.TestCase):
    def test_many_versions_share_one_explicit_job_budget(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            script = Path(sysconfig.get_path("scripts")) / "unbake"
            launcher = directory / "cli.py"
            # Only identify the synthetic IPL3; all setup and build code is installed.
            launcher.write_text(
                "import runpy, sys, zlib\n"
                "from unbake.project import header\n"
                "header.RETAIL[zlib.crc32(bytes(0xfc0))] = '6102/7101'\n"
                f"sys.argv[0] = {str(script)!r}\n"
                f"runpy.run_path({str(script)!r}, run_name='__main__')\n"
            )
            with Path(os.environ["UNBAKE_POLICY"]).open("rb") as stream:
                settings = tomllib.load(stream)
            settings.update(cores=12, setup_version_jobs=2)
            policy = directory / "policy.toml"
            policy.write_text("".join(f"{key} = {json.dumps(value)}\n" for key, value in settings.items()))
            environment = dict(os.environ, UNBAKE_POLICY=str(policy), PYTHONNOUSERSITE="1")
            environment.pop("PYTHONPATH", None)
            samples = []

            def command(*arguments):
                output = directory / "command.log"
                peak = active = 0
                started = time.monotonic()
                with output.open("w") as log:
                    process = subprocess.Popen(
                        [sys.executable, str(launcher), *arguments],
                        cwd=directory,
                        env=environment,
                        stdin=subprocess.DEVNULL,
                        stdout=log,
                        stderr=subprocess.STDOUT,
                        start_new_session=True,
                    )
                    try:
                        while process.poll() is None:
                            resident = jobs = 0
                            for path in Path("/proc").glob("[0-9]*/stat"):
                                try:
                                    fields = path.read_text().rsplit(")", 1)[1].split()
                                    if int(fields[2]) != process.pid:
                                        continue
                                    resident += int(fields[21]) * os.sysconf("SC_PAGE_SIZE")
                                    words = (path.parent / "cmdline").read_bytes().split(b"\0")
                                    jobs += bool(words[0] == b"make" and b"extract" in words)
                                except (OSError, ValueError, IndexError):
                                    continue
                            peak, active = max(peak, resident), max(active, jobs)
                            self.assertLess(resident, 1_000_000_000, "setup exceeded the test's resident memory guard")
                            self.assertLess(time.monotonic() - started, 180, "setup timed out")
                            time.sleep(0.025)
                    finally:
                        if process.poll() is None:
                            os.killpg(process.pid, signal.SIGTERM)
                            try:
                                process.wait(timeout=3)
                            except subprocess.TimeoutExpired:
                                os.killpg(process.pid, signal.SIGKILL)
                        process.wait()
                return process.returncode, output.read_text(), peak, active, time.monotonic() - started

            # A real boot clear loop establishes the loaded range; one leaf is shared.
            boot = [0x3C088000, 0x25082180, 0x3C098000, 0x25292200, 0x25080004, 0x0109082B, 0x1420FFFD, 0xAD00FFFC]
            boot.extend([0x0C000440, 0, 0x03E00008, 0])
            code = bytearray(0x180)
            struct.pack_into(f">{len(boot)}I", code, 0, *boot)
            struct.pack_into(">3I", code, 0x100, 0x24020001, 0x03E00008, 0)
            for count in (2, 6, 18):
                project = directory / f"Memory{count}"
                status, output, *_ = command("init", str(project))
                self.assertEqual(status, 0, output)
                for revision in range(count):
                    data = cartridge(revision=revision, seed=0x3C, instructions=bytes(code))
                    path = project / "roms" / f"input{revision}"
                    with path.open("wb") as stream:
                        stream.write(data)
                        stream.truncate(16 * 1024 * 1024)
                status, output, proposal_peak, _, _ = command("--project", str(project), "setup", "--names-from", "us")
                self.assertEqual(status, 1, output)
                self.assertIn("setup.compiler_confirmation", output)
                proposal = project / "build/setup/proposal.json"
                token = hashlib.sha256(proposal.read_bytes()).hexdigest()
                status, output, peak, active, seconds = command("--project", str(project), "setup", "--confirm", token)
                self.assertEqual(status, 0, output)
                self.assertEqual(output.count("every cartridge byte proved"), count)
                self.assertLessEqual(active, 2)
                with (project / "config.toml").open("rb") as stream:
                    self.assertEqual(tomllib.load(stream)["project"]["state"], "ready")
                samples.append((count, proposal_peak, peak, active, round(seconds, 3)))
            # 9x as many cartridge bytes must not create 9x resident work.
            # Allow host/process sampling variation, while catching retained ROMs
            # and concurrent extractors from the previous scheduling boundary.
            self.assertLess(samples[-1][1], samples[0][1] + 64 * 1024 * 1024, samples)
            self.assertLess(samples[-1][2], samples[0][2] + 96 * 1024 * 1024, samples)
            print("setup memory (versions, proposal RSS, confirm RSS, extraction jobs, seconds):", samples)
