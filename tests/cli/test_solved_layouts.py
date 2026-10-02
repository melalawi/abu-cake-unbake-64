"""Installed map, solve and draft share an evidenced partial storage layout."""

import hashlib
import json
import os
import shutil
import subprocess
import sysconfig
import tempfile
import unittest
from dataclasses import asdict
from pathlib import Path

import toml

from tests.decomp.support import fixture
from unbake.decomp.checks import run as source_rules
from unbake.project.config import load_policy


class SolvedLayoutCliTests(unittest.TestCase):
    def test_installed_solve_and_draft_use_global_storage_evidence(self):
        with tempfile.TemporaryDirectory(dir=os.environ["TMPDIR"]) as directory:
            project, _, _ = fixture(Path(directory), words=[0x3C088000, 0x8D083000, 0x8D020004, 0x03E00008, 0])
            (project.asm / "us/nonmatchings/alpha.s").write_text(
                ".text\nglabel alpha\n lui $t0, %hi(source)\n lw $t0, %lo(source)($t0)\n"
                " lw $v0, 4($t0)\n jr $ra\n nop\n"
            )
            config = toml.loads((Path(__file__).parents[1] / "fixture/config.toml").read_text())
            config["project"].update(versions=["us"], names_from="us")
            cartridge = project.version("us")
            image = bytearray(cartridge.baserom.read_bytes())
            image[0x54:0x60] = bytes.fromhex("3c0880008d0830008d020008")
            cartridge.baserom.write_bytes(image)
            project.roms.mkdir()
            destination = project.roms / "baserom.us.z64"
            cartridge.baserom.rename(destination)
            config["version"] = {
                "us": {
                    "baserom": str(destination.relative_to(project.root)),
                    "baserom_sha1": hashlib.sha1(image).hexdigest(),
                    "split": str(cartridge.split.relative_to(project.root)),
                    "symbols": str(cartridge.symbols.relative_to(project.root)),
                    "macros": [],
                }
            }
            config["compilers"]["ido-7.1"]["cflags"] = []
            (project.root / "config.toml").write_text(toml.dumps(config))
            cartridge.symbols.write_text(cartridge.symbols.read_text() + "source = 0x80003000;\n")
            (project.include[0] / "types.h").write_text("typedef int s32; extern int source;\n")
            compiler = project.tools / "ido-7.1/cc"
            compiler.parent.mkdir()
            shutil.copyfile(project.compilers["ido-7.1"].cc, compiler)
            compiler.chmod(0o755)
            shutil.copyfile(project.compilers["ido-7.1"].as_, compiler.parent / "as1")
            (compiler.parent / "as1").chmod(0o755)
            policy = load_policy()
            environment = dict(os.environ, PYTHONNOUSERSITE="1")
            environment.pop("PYTHONPATH", None)
            script = Path(sysconfig.get_path("scripts")) / "unbake"

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

            self.assertTrue(policy.m2c.is_file())
            cli("map")
            cli("solve")
            database = json.loads((project.build / "types/database.json").read_text())
            shapes = {name: row for name, row in database["structs"].items() if row["state"] == "known"}
            self.assertEqual(len(shapes), 1)
            name, shape = next(iter(shapes.items()))
            self.assertTrue(name.startswith("Shape_"))
            self.assertEqual(shape["common_base"], "global:source")
            self.assertEqual(shape["minimum_size"], 12)
            self.assertIsNone(shape["size"])
            self.assertIn("padding_0[4]", shape["declaration"])
            self.assertEqual(database["globals"]["source"]["type"], "int")
            header = project.include[0] / "shared/typemap.h"
            before = header.read_bytes()
            cli("draft", "alpha")
            draft = (project.drafts / "alpha/alpha.c").read_text()
            self.assertIn("->field_4", draft)
            self.assertIn("shared/prototypes.h", draft)
            self.assertNotIn("M2C_", draft)
            self.assertNotIn("typedef", draft)
            self.assertFalse([finding for finding in source_rules(draft) if finding.rule == "raw-offset"])
            self.assertEqual(header.read_bytes(), before)
            protected = [project.build / "types" / path for path in ("database.json", "summary.json", "redraft.json")]
            protected.extend(project.include[0] / "shared" / path for path in ("typemap.h", "prototypes.h"))
            previous = {path: path.read_bytes() for path in protected}
            # A parser refusal at publication must preserve the last revision.
            parser = project.tools / "reject-context"
            parser.write_text("#!/bin/sh\nprintf 'invalid shared context' >&2\nexit 1\n")
            parser.chmod(0o755)
            values = asdict(policy)
            values["m2c"] = parser
            failing_policy = project.root / "parser-policy.toml"
            failing_policy.write_text(
                toml.dumps({key: str(value) if isinstance(value, Path) else value for key, value in values.items()})
            )
            environment["UNBAKE_POLICY"] = str(failing_policy)
            output = cli("solve", expected=1)
            self.assertIn("types.header_parse", output)
            self.assertEqual(previous, {path: path.read_bytes() for path in protected})
