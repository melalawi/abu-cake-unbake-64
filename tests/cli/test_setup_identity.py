"""Installed setup establishes identity from synthetic cartridge instructions."""

import json
import os
import struct
import subprocess
import sys
import sysconfig
import tempfile
import unittest
from pathlib import Path

from tests.project.test_rom import cartridge


class SetupIdentityTests(unittest.TestCase):
    def test_indexed_addresses_repeated_stubs_and_changed_bodies(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            project = directory / "Identity"
            script = Path(sysconfig.get_path("scripts")) / "unbake"
            environment = dict(os.environ, PYTHONNOUSERSITE="1")
            environment.pop("PYTHONPATH", None)
            # The only substituted boundary is IPL3 identification. Code census,
            # disassembly, layout, correspondence and proposal run normally.
            launcher = directory / "cli.py"
            launcher.write_text(
                "import runpy, sys, zlib\n"
                "from unbake.project import header\n"
                "header.RETAIL[zlib.crc32(bytes(0xfc0))] = '6102/7101'\n"
                f"sys.argv[0] = {str(script)!r}\n"
                f"runpy.run_path({str(script)!r}, run_name='__main__')\n"
            )

            def command(*arguments):
                return subprocess.run(
                    [sys.executable, str(launcher), *arguments],
                    env=environment,
                    cwd=directory,
                    capture_output=True,
                    text=True,
                    timeout=60,
                    check=False,
                )

            result = command("init", str(project))
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            for region, high, low, changed in (("E", 0x8001, 0x1234, 3), ("P", 0x8002, 0x2348, 4)):
                code = bytearray(0x180)
                boot = [0x3C088000, 0x25082180, 0x3C098000, 0x25292200, 0x25080004, 0x0109082B, 0x1420FFFD, 0xAD00FFFC]
                for at in range(0x1100, 0x1170, 0x10):
                    boot.extend([0x0C000000 | ((0x80000000 + at) >> 2 & 0x03FFFFFF), 0])
                boot.extend([0x03E00008, 0])
                struct.pack_into(f">{len(boot)}I", code, 0, *boot)
                # The array body occupies two slots; do not seed its interior.
                array = [0x3C010000 | high, 0x00220821, 0x8C220000 | low, 0x03E00008, 0]
                struct.pack_into(">5I", code, 0x100, *array)
                for at, value in ((0x120, 1), (0x150, 2), (0x160, changed)):
                    struct.pack_into(">3I", code, at, 0x24020000 | value, 0x03E00008, 0)
                for at in (0x130, 0x140):
                    struct.pack_into(">2I", code, at, 0x03E00008, 0)
                # Remove the call to the array's interior at 0x1110.
                struct.pack_into(">2I", code, 0x28, 0, 0)
                (project / "roms" / region).write_bytes(cartridge(region=region, seed=0x3C, instructions=bytes(code)))
            result = command("--project", str(project), "setup", "--names-from", "us")
            output = result.stdout + result.stderr
            self.assertNotIn("Traceback", output)
            self.assertIn("setup.compiler_confirm", output)
            layout = json.loads((project / "build/setup/layout.json").read_text())
            ff = {v: {f["start"]: f for f in row["functions"]} for v, row in layout["versions"].items()}
            self.assertIn(0x1100, ff["us"], {v: [(hex(at), f["name"]) for at, f in fs.items()] for v, fs in ff.items()})
            for at in (0x1100, 0x1130, 0x1140):
                self.assertEqual(ff["us"][at]["name"], ff["eu"][at]["name"])
            self.assertNotEqual(ff["us"][0x1130]["name"], ff["us"][0x1140]["name"])
            self.assertEqual(ff["us"][0x1130]["evidence"]["correspondence"], "anchor-sequence")
            self.assertNotEqual(ff["us"][0x1160]["name"], ff["eu"][0x1160]["name"])
            self.assertEqual(ff["us"][0x1160]["evidence"]["correspondence"], "body-not-shared")
