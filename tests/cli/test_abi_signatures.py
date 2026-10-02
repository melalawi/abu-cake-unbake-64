"""Installed solve and draft retain named semantic conflicts without blocking callers."""

import hashlib
import json
import os
import shutil
import struct
import subprocess
import sysconfig
import tempfile
import unittest
from dataclasses import asdict
from pathlib import Path

import toml

from tests.decomp.support import fixture
from unbake.project.config import load_policy


class AbiSignatureCliTests(unittest.TestCase):
    def test_installed_conflicting_callee_types_draft_and_parse_rollback(self):
        with tempfile.TemporaryDirectory(dir=os.environ["TMPDIR"]) as directory:
            # alpha forwards an integer; gamma tail-forwards a pointer to beta.
            words = [0x27BDFFE8, 0xAFBF0014, 0, 0, 0x24050002, 0x8FBF0014, 0x27BD0018, 0x03E00008, 0]
            address = 0x80001000 + len(words) * 4
            words[3] = 0x0C000000 | ((address >> 2) & 0x3FFFFFF)
            project, _, _ = fixture(Path(directory), words=words)
            (project.asm / "us/nonmatchings/alpha.s").write_text(
                ".text\nglabel alpha\n addiu $sp,$sp,-24\n sw $ra,20($sp)\n nop\n"
                " jal beta\n addiu $a1,$zero,2\n lw $ra,20($sp)\n"
                " addiu $sp,$sp,24\n jr $ra\n nop\n"
            )
            cartridge = project.version("us")
            image = bytearray(cartridge.baserom.read_bytes())
            offset = 0x40 + len(words) * 4
            image[offset : offset + 12] = struct.pack(">3I", 0x00851021, 0x03E00008, 0)
            image[offset + 12 : offset + 24] = struct.pack(">3I", 0x08000000 | ((address >> 2) & 0x3FFFFFF), 0, 0)
            project.roms.mkdir()
            rom = project.roms / "baserom.us.z64"
            rom.write_bytes(image)
            config = toml.loads((Path(__file__).parents[1] / "fixture/config.toml").read_text())
            config["project"].update(versions=["us"], names_from="us")
            config["version"] = {
                "us": {
                    "baserom": "roms/baserom.us.z64",
                    "baserom_sha1": hashlib.sha1(image).hexdigest(),
                    "split": str(cartridge.split.relative_to(project.root)),
                    "symbols": str(cartridge.symbols.relative_to(project.root)),
                    "macros": [],
                }
            }
            config["compilers"]["ido-7.1"]["cflags"] = []
            (project.root / "config.toml").write_text(toml.dumps(config))
            (project.include[0] / "types.h").write_text(
                "typedef int s32; int alpha(int value); int gamma(float *value);\n"
            )
            compiler = project.tools / "ido-7.1/cc"
            compiler.parent.mkdir()
            shutil.copyfile(project.compilers["ido-7.1"].cc, compiler)
            compiler.chmod(0o755)
            shutil.copyfile(project.compilers["ido-7.1"].as_, compiler.parent / "as1")
            (compiler.parent / "as1").chmod(0o755)
            script = Path(sysconfig.get_path("scripts")) / "unbake"
            environment = dict(os.environ, PYTHONNOUSERSITE="1")
            environment.pop("PYTHONPATH", None)

            def cli(*arguments, expected=0):
                result = subprocess.run(
                    [str(script), "--project", str(project.root), *arguments],
                    env=environment,
                    capture_output=True,
                    text=True,
                    timeout=30,
                    check=False,
                )
                self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
                return result.stdout + result.stderr

            cli("map")
            map_path = project.build / "map/facts.json"
            original_map = map_path.read_bytes()
            cli("solve")
            database = json.loads((project.build / "types/database.json").read_text())
            callee = database["functions"]["beta"]
            self.assertEqual(callee["state"], "conflict")
            self.assertIsNone(callee["params"][0]["type"])
            self.assertIsNone(callee["prototype"])
            self.assertEqual(callee["abi_declaration"]["prototype"], "int beta(int, int);")
            self.assertEqual(callee["abi"]["call_sites"], 2)
            cli("draft", "alpha")
            source = (project.drafts / "alpha/alpha.c").read_text()
            self.assertIn("extern int beta(int, int);", source)
            self.assertIn("types.abi.word:", source)
            self.assertNotIn("M2C_UNK", source)
            self.assertNotIn("typedef", source)
            self.assertEqual(map_path.read_bytes(), original_map)
            cli("solve")
            repeated = json.loads((project.build / "types/database.json").read_text())
            self.assertEqual(repeated["functions"]["beta"]["abi_declaration"], callee["abi_declaration"])
            protected = [project.build / "types" / name for name in ("database.json", "summary.json", "redraft.json")]
            protected.extend(project.include[0] / "shared" / name for name in ("typemap.h", "prototypes.h"))
            previous = {path: path.read_bytes() for path in protected}
            parser = project.tools / "reject-context"
            parser.write_text("#!/bin/sh\nprintf 'invalid shared context' >&2\nexit 1\n")
            parser.chmod(0o755)
            values = asdict(load_policy())
            values["m2c"] = parser
            policy = project.root / "reject-policy.toml"
            policy.write_text(
                toml.dumps({key: str(value) if isinstance(value, Path) else value for key, value in values.items()})
            )
            environment["UNBAKE_POLICY"] = str(policy)
            self.assertIn("types.header_parse", cli("solve", expected=1))
            self.assertEqual(previous, {path: path.read_bytes() for path in protected})
