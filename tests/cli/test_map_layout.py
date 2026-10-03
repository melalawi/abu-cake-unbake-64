"""Installed layout commands and solve refresh only affected instruction facts."""

import hashlib
import json
import os
import struct
import subprocess
import sysconfig
import tempfile
import unittest
from pathlib import Path

import toml

from tests.decomp.support import fixture
from tests.process_fakes import cli_process
from unbake.project import toolchain
from unbake.typemap import shards


class MapLayoutCliTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(dir=os.environ["TMPDIR"])
        self.addCleanup(temporary.cleanup)
        words = [0x3C088000, 0x8D083000, 0x8D020004, 0x0C000408, 0, 0x03E00008, 0, 0]
        project, _, _ = fixture(Path(temporary.name).resolve(), words=words, case=self)
        self.root = project.root
        cartridge = project.version("us")
        cartridge.split.write_text(cartridge.split.read_text().replace("asm, beta", 'data, "rodata/80001020"'))
        config = toml.loads((Path(__file__).parents[1] / "fixture/config.toml").read_text())
        config["project"].update(versions=["us"], names_from="us")
        project.roms.mkdir()
        rom = project.roms / "baserom.us.z64"
        cartridge.baserom.rename(rom)
        config["version"] = {
            "us": {
                "baserom": "roms/baserom.us.z64",
                "baserom_sha1": hashlib.sha1(rom.read_bytes()).hexdigest(),
                "split": str(cartridge.split.relative_to(project.root)),
                "symbols": str(cartridge.symbols.relative_to(project.root)),
                "macros": [],
            }
        }
        (project.root / "config.toml").write_text(toml.dumps(config))
        # A tiny proof adapter keeps these tests focused on CLI transaction and
        # map behaviour. Cartridge extraction is separately covered by real builds.
        from unittest.mock import patch

        from tests.process_fakes import boundary
        from unbake.layout import code_interval
        from unbake.project import build

        def decode(command, **kwargs):
            assert command[1:4] == ["-D", "-z", "-b"], command
            words = struct.unpack(f">{Path(command[-1]).stat().st_size // 4}I", Path(command[-1]).read_bytes())
            text = "\n".join(
                f"{i * 4:x}: {word:08x} " + (".word" if word == 1 else "instruction") for i, word in enumerate(words)
            )
            return subprocess.CompletedProcess(command, 0, text, "")

        def proof(project, policy, versions, *, tree, generation_for):
            rows = []
            for version in versions:
                generation = generation_for(version)
                generation.mkdir(parents=True, exist_ok=True)
                (generation / f"{project.name}.{version}.z64").write_bytes(
                    project.version(version).baserom.read_bytes()
                )
                log = generation / "build.log"
                log.write_text("fixture proof")
                rows.append(build.BuildResult(version, not (tree / "refuse").exists(), "fixture: OK", log, generation))
            return {row.version: row for row in rows}

        for mock in (
            boundary(code_interval, decode),
            patch.object(build, "build", side_effect=proof),
            patch.object(toolchain, "ensure"),
            patch.object(toolchain, "verify", return_value={}),
        ):
            mock.start()
            self.addCleanup(mock.stop)
        self.environment = dict(os.environ, PYTHONNOUSERSITE="1")
        self.environment.pop("PYTHONPATH", None)
        self.script = Path(sysconfig.get_path("scripts")) / "unbake"
        self.symbols = cartridge.symbols
        self.layout = cartridge.split
        self.map_path = project.build / "map/facts.json"

    def cli(self, *arguments, expected=0):
        result = cli_process(
            [str(self.script), "--project", str(self.root), *arguments],
            env=self.environment,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        output = result.stdout + result.stderr
        self.assertEqual(result.returncode, expected, output)
        self.assertEqual(sum(line.startswith("Next: ") for line in output.splitlines()), 1, output)
        return output

    def manifest(self):
        return json.loads(self.map_path.read_bytes())

    def bodies(self, value):
        return dict(shards.Functions(self.map_path.parent / value["shard"], value["functions"]))

    def test_code_symbol_and_solve_match_full_map_without_rescanning_neighbours(self):
        self.cli("map")
        self.cli("solve")
        initial = self.manifest()
        original = self.bodies(initial)
        refused = self.cli(
            "split", "cut", "recovered", "--version", "us", "--start", "0x60", "--end", "0x6c", expected=1
        )
        self.assertIn("split.data_to_code", refused)
        self.assertIn("Next: unbake", refused)
        self.assertIn("split code FUNCTION", refused)
        self.assertNotIn("Supply", refused)
        preview = self.cli("split", "code", "recovered", "--version", "us", "--start", "0x60", "--end", "0x6c")
        self.assertIn("direct-code-target", preview)
        self.assertIn("gapless-partition", preview)
        self.assertIn('data, "rodata/80001020"', self.layout.read_text())
        self.cli("split", "code", "recovered", "--version", "us", "--start", "0x60", "--end", "0x6c", "--apply")
        self.cli("split", "data-symbol", "source", "--version", "us", "--address", "0x80003000", "--apply")
        output = self.cli("solve")
        self.assertNotIn("inputs_stale", output)
        refreshed = self.manifest()
        self.assertEqual(refreshed["refresh"]["rescanned"], 2)
        self.assertEqual(refreshed["refresh"]["reused"], 1)
        bodies = self.bodies(refreshed)
        self.assertEqual(bodies["gamma"], original["gamma"])
        self.assertEqual(bodies["alpha"]["versions"]["us"]["calls"][0]["callee"], "recovered")
        self.assertIn("global:source", str(bodies["alpha"]))
        self.cli("map")
        complete = self.manifest()
        self.assertEqual(self.bodies(complete), bodies)
        self.assertEqual(complete["globals"], refreshed["globals"])
        self.assertEqual(complete["pools"], refreshed["pools"])
        self.cli(
            "split",
            "data-symbol",
            "new_source",
            "--version",
            "us",
            "--address",
            "0x80003000",
            "--rename-from",
            "source",
            "--apply",
        )
        self.cli("solve")
        renamed = self.manifest()
        self.assertEqual(renamed["refresh"]["rescanned"], 1)
        self.assertIn("global:new_source", str(self.bodies(renamed)["alpha"]))
        self.assertNotIn("source", renamed["globals"])

    def test_refused_code_proof_keeps_layout_symbols_generation_and_map(self):
        self.cli("map")
        protected = [self.layout, self.symbols, self.map_path]
        before = {p: p.read_bytes() for p in protected}
        link = self.root / "build/us"
        target = os.readlink(link)
        (self.root / "refuse").touch()
        output = self.cli(
            "split", "code", "recovered", "--version", "us", "--start", "0x60", "--end", "0x6c", "--apply", expected=1
        )
        self.assertIn("HELD(split)", output)
        self.assertEqual(before, {p: p.read_bytes() for p in protected})
        self.assertEqual(os.readlink(link), target)
        invalid = self.cli(
            "split", "code", "recovered", "--version", "us", "--start", "0x64", "--end", "0x6c", expected=1
        )
        self.assertIn("split.code.reference", invalid)
        self.assertIn("data-to-code correction", invalid)
        self.assertNotIn("Supply", invalid)
        self.assertEqual(before, {p: p.read_bytes() for p in protected})

    def test_numeric_boundary_map_owner_names_the_data_to_code_operation(self):
        changes = self.root / "boundaries.json"
        changes.write_text(
            json.dumps(
                [
                    {
                        "version": "us",
                        "function": "80001020",
                        "action": "code",
                        "sha256": "0" * 64,
                        "evidence": "referenced code body",
                    }
                ]
            )
        )
        before = self.layout.read_bytes()
        output = self.cli("split", "boundary-map", str(changes), expected=1)
        self.assertIn("split.data_to_code", output)
        self.assertIn("split code FUNCTION", output)
        self.assertNotIn("Supply", output)
        self.assertEqual(self.layout.read_bytes(), before)

    def test_referenced_pointer_table_and_closed_body_are_both_required(self):
        rom = self.root / "roms/baserom.us.z64"
        image = bytearray(rom.read_bytes())
        image[0x40:0x60] = struct.pack(">8I", 0x3C088000, 0x25081038, 0, 0, 0, 0x03E00008, 0, 0)
        image.extend(struct.pack(">3I", 0x80001000, 0x80001020, 0x8000102C))
        self.layout.write_text(
            self.layout.read_text().replace("  - [0x78]", '      - [0x78, data, "rodata/table"]\n  - [0x84]')
        )

        def pin():
            rom.write_bytes(image)
            config = toml.loads((self.root / "config.toml").read_text())
            config["version"]["us"]["baserom_sha1"] = hashlib.sha1(image).hexdigest()
            (self.root / "config.toml").write_text(toml.dumps(config))

        pin()
        arguments = ("split", "code", "recovered", "--version", "us", "--start", "0x60", "--end", "0x6c")
        output = self.cli(*arguments)
        self.assertIn("referenced-code-pointer-table", output)
        before = self.layout.read_bytes()
        image[0x64:0x68] = bytes.fromhex("ffffffff")
        pin()
        self.assertIn("split.code.control_flow", self.cli(*arguments, expected=1))
        self.assertEqual(self.layout.read_bytes(), before)
        image[0x64:0x68] = bytes.fromhex("03e00008")
        image[0x60:0x64] = bytes.fromhex("00000001")
        pin()
        self.assertIn("split.code.decode", self.cli(*arguments, expected=1))
        self.assertEqual(self.layout.read_bytes(), before)
        image[0x60:0x64] = bytes.fromhex("24020002")
        image[0x40:0x48] = bytes(8)
        pin()
        self.assertIn("split.code.reference", self.cli(*arguments, expected=1))
        self.assertEqual(self.layout.read_bytes(), before)
