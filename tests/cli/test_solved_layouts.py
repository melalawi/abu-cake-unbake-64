"""Installed map, solve and draft share an evidenced partial storage layout."""

import hashlib
import json
import os
import shutil
import sysconfig
import tempfile
import unittest
from pathlib import Path

import toml

from tests.decomp.support import fixture
from tests.process_fakes import cli_process
from unbake.decomp.checks import run as source_rules
from unbake.project.config import load_policy


class SolvedLayoutCliTests(unittest.TestCase):
    def test_installed_solve_and_draft_use_global_storage_evidence(self):
        self.m2c_output = "int alpha(void) { return M2C_FIELD(source, int *, 4); }\n"
        with tempfile.TemporaryDirectory(dir=os.environ["TMPDIR"]) as directory:
            project, _, _ = fixture(
                Path(directory).resolve(), words=[0x3C088000, 0x8D083000, 0x8D020004, 0x03E00008, 0], case=self
            )
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
                if arguments and arguments[0] == "draft":
                    arguments = (*arguments, "--scratch", str(Path(directory) / "draft-scratch"))
                result = cli_process(
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
            # Restore the fixture's fixed-return C receipt. Its included
            # generated declarations must not erase source bindings on solve.
            published = project.src / "gamma.c"
            published.write_text('#include "shared/typemap.h"\nint gamma(void) { return 3; }\n')
            cartridge.split.write_text(cartridge.split.read_text().replace(", asm, gamma]", ", c, gamma]"))
            cli("map")
            mapped = json.loads((project.build / "map/facts.json").read_text())
            source_sha256 = hashlib.sha256(published.read_bytes()).hexdigest()
            proof = {
                "matched": True,
                "source_sha256": source_sha256,
                "versions": ["us"],
                "target_sha256": {"us": mapped["functions"]["gamma"]["versions"]["us"]["target_sha256"]},
            }
            receipt = {key: database[key] for key in ("schema", "project_id", "workspace_id", "rom_sha1")}
            receipt["records"] = {
                "gamma": {"source": "src/gamma.c", "source_sha256": source_sha256, "versions": ["us"], "proof": proof}
            }
            (project.build / "types/proven.json").write_text(json.dumps(receipt))
            cli("solve")
            cli("solve")
            repeated = json.loads((project.build / "types/database.json").read_text())["structs"][name]
            self.assertEqual(repeated["common_base"], shape["common_base"])
            self.assertEqual(repeated["base_nodes"], shape["base_nodes"])
            self.assertIsNone(repeated["size"])
            header = project.include[0] / "shared/typemap.h"
            before = header.read_bytes()
            cli("draft", "alpha")
            draft = (Path(directory) / "draft-scratch/drafts/alpha/alpha.c").read_text()
            self.assertIn("->field_4", draft)
            self.assertIn("shared/prototypes.h", draft)
            self.assertNotIn("M2C_", draft)
            self.assertNotIn("typedef", draft)
            self.assertFalse([finding for finding in source_rules(draft) if finding.rule == "raw-offset"])
            self.assertEqual(header.read_bytes(), before)
            # Original SDK callback contracts (with unnamed scalar parameters)
            # must pass the installed solver's header publication parser.
            from unbake.project import setup

            scalar = project.include[0] / "types.h"
            scalar.write_text(scalar.read_text() + "typedef unsigned int u32; typedef unsigned long long u64;\n")
            setup._sdk_headers(project)
            cli("solve")
            callbacks = project.include[0] / "shared/audio_callbacks.h"
            self.assertEqual(callbacks.read_bytes(), (setup.makefile.TEMPLATES / "audio_callbacks.h").read_bytes())
            protected = [project.build / "types" / path for path in ("database.json", "summary.json", "redraft.json")]
            protected.extend(project.include[0] / "shared" / path for path in ("typemap.h", "prototypes.h"))
            previous = {path: path.read_bytes() for path in protected}
            # A parser refusal at publication must preserve the last revision.
            broken = project.include[0] / "types.h"
            broken.write_text(broken.read_text() + "\nthis is invalid C;\n")
            output = cli("solve", expected=1)
            self.assertIn("header declaration", output)
            self.assertEqual(previous, {path: path.read_bytes() for path in protected})
