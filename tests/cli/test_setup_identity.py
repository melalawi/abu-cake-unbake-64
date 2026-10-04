"""Installed setup establishes identity from synthetic cartridge instructions."""

import hashlib
import json
import os
import struct
import sys
import sysconfig
import tempfile
import unittest
from pathlib import Path

import toml

from tests.process_fakes import cli_process
from tests.project.test_rom import cartridge


class SetupIdentityTests(unittest.TestCase):
    def setUp(self):
        from tests.rom_fixture import install

        install(self)
        from tests.setup_fixture import install as setup_tools

        setup_tools(self)

    def test_indexed_addresses_repeated_stubs_and_changed_bodies(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary).resolve()
            project = directory / "Identity"
            script = Path(sysconfig.get_path("scripts")) / "unbake"
            values = toml.loads(Path(os.environ["UNBAKE_POLICY"]).read_text())
            values.update(symbol_similarity_threshold=0.9, symbol_similarity_margin=0.1)
            policy = directory / "policy.toml"
            policy.write_text(toml.dumps(values))
            environment = dict(os.environ, PYTHONNOUSERSITE="1", UNBAKE_POLICY=str(policy))
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
                return cli_process(
                    [sys.executable, str(launcher), *arguments],
                    env=environment,
                    cwd=directory,
                    capture_output=True,
                    text=True,
                    timeout=60,
                    check=False,
                )

            result = command("init", str(project), "--layout-cap", "2")
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
            result = command("--project", str(project), "setup", "--names-from", "us", "--compiler", "default=ido-7.1")
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

    def test_changed_body_has_one_symbol_and_shared_caller_compiles_in_both_versions(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary).resolve()
            project = directory / "Symbols"
            script = Path(sysconfig.get_path("scripts")) / "unbake"
            values = toml.loads(Path(os.environ["UNBAKE_POLICY"]).read_text())
            values.update(symbol_similarity_threshold=0.9, symbol_similarity_margin=0.1)
            policy = directory / "policy.toml"
            policy.write_text(toml.dumps(values))
            environment = dict(os.environ, PYTHONNOUSERSITE="1", UNBAKE_POLICY=str(policy))
            environment.pop("PYTHONPATH", None)
            launcher = directory / "cli.py"
            launcher.write_text(
                "import runpy, sys, zlib\n"
                "from unbake.project import header\n"
                "header.RETAIL[zlib.crc32(bytes(0xfc0))] = '6102/7101'\n"
                f"sys.argv[0] = {str(script)!r}\n"
                f"runpy.run_path({str(script)!r}, run_name='__main__')\n"
            )

            def command(*arguments, expected=0):
                if "try" in arguments:
                    arguments = (*arguments, "--scratch", str(directory / "scratch"))
                result = cli_process(
                    [sys.executable, str(launcher), *arguments],
                    env=environment,
                    cwd=directory,
                    capture_output=True,
                    text=True,
                    timeout=90,
                    check=False,
                )
                self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
                return result.stdout + result.stderr

            command("init", str(project), "--layout-cap", "2")
            # KMC O2: x+3 is two words; (x+3)&255 adds an andi word.
            # The caller uses exactly one C identifier for both body variants.
            for region, body in (("E", [0x03E00008, 0x24820003]), ("P", [0x24820003, 0x03E00008, 0x304200FF])):
                code = bytearray(0x180)
                boot = [0x3C088000, 0x25082180, 0x3C098000, 0x25292200, 0x25080004, 0x0109082B, 0x1420FFFD, 0xAD00FFFC]
                for at in (0x1100, 0x1120, 0x1140, 0x1160):
                    boot.extend([0x0C000000 | ((0x80000000 + at) >> 2 & 0x03FFFFFF), 0])
                boot.extend([0x03E00008, 0])
                struct.pack_into(f">{len(boot)}I", code, 0, *boot)
                struct.pack_into(">3I", code, 0x100, 0x3C028001, 0x03E00008, 0)
                struct.pack_into(f">{len(body)}I", code, 0x120, *body)
                struct.pack_into(">3I", code, 0x140, 0x24020002, 0x03E00008, 0)
                caller = [0x27BDFFE8, 0xAFBF0010, 0x0C000448, 0, 0x8FBF0010, 0x24420001, 0x03E00008, 0x27BD0018]
                struct.pack_into(">8I", code, 0x160, *caller)
                (project / "roms" / region).write_bytes(cartridge(region=region, seed=0x3C, instructions=bytes(code)))
            command("--project", str(project), "setup", "--names-from", "us", expected=1)
            layout = json.loads((project / "build/setup/layout.json").read_text())
            ff = {v: {f["start"]: f for f in row["functions"]} for v, row in layout["versions"].items()}
            callee = ff["us"][0x1120]["name"]
            caller = ff["us"][0x1160]["name"]
            self.assertEqual(callee, ff["eu"][0x1120]["name"])
            self.assertEqual(caller, ff["eu"][0x1160]["name"])
            self.assertEqual(ff["eu"][0x1120]["evidence"]["correspondence"], "anchor-call-graph")
            self.assertEqual(len(layout["items"][callee]["body_groups"]), 2)
            self.assertNotEqual(ff["us"][0x1120]["body_sha256"], ff["eu"][0x1120]["body_sha256"])
            # Select the measured fixture recipe for all units through setup.
            arguments = ["--project", str(project), "setup"]
            for name in layout["items"]:
                arguments.extend(["--compiler", name + "=gcc-2.7.2-kmc"])
            command(*arguments, expected=1)
            token = hashlib.sha256((project / "build/setup/proposal.json").read_bytes()).hexdigest()
            command("--project", str(project), "setup", "--confirm", token)
            for version in ("us", "eu"):
                symbols = (project / "versions" / version / "symbol_addrs.txt").read_text()
                self.assertIn(callee + " = ", symbols)
            # Represent a ready fixture made by the body-only planner:
            # its EU body has a separate name, while the shared caller stays C-expressible.
            other = "func_80001120_us_separate"
            split_path = project / "versions/us/Symbols.yaml"
            split_path.write_text(split_path.read_text().replace('"' + callee + '"', '"' + other + '"'))
            symbol_path = project / "versions/us/symbol_addrs.txt"
            symbol_path.write_text(symbol_path.read_text().replace(callee + " = ", other + " = "))
            configuration = toml.loads((project / "config.toml").read_text())
            # [units] lists only exception units; carry the callee's entry when it has one.
            units = configuration.get("units", {})
            if callee in units:
                units[other] = units[callee]
            (project / "config.toml").write_text(toml.dumps(configuration))
            output = command("--project", str(project), "setup", "--replan-symbols")
            token = output.split("setup --replan-symbols --confirm ", 1)[1].split()[0]
            proposal = json.loads((project / "build/setup/symbol-proposal.json").read_text())
            self.assertEqual(proposal["replacements"], {other: callee})
            command("--project", str(project), "setup", "--replan-symbols", "--confirm", "0" * 64, expected=1)
            self.assertIn(other + " = ", symbol_path.read_text())
            affected = project / "src" / (other + ".c")
            affected.parent.mkdir(exist_ok=True)
            affected.write_text(f"int {other}(int x) {{ return x + 3; }}\n")
            output = command("--project", str(project), "setup", "--replan-symbols")
            affected_token = output.split("setup --replan-symbols --confirm ", 1)[1].split()[0]
            output = command(
                "--project", str(project), "setup", "--replan-symbols", "--confirm", affected_token, expected=1
            )
            self.assertIn("setup.symbol_authored_source", output)
            self.assertIn(other + " = ", symbol_path.read_text())
            affected.unlink()
            output = command("--project", str(project), "setup", "--replan-symbols", "--confirm", token)
            self.assertEqual(output.count("every cartridge byte proved"), 2)
            self.assertIn('"' + callee + '"', split_path.read_text())
            self.assertNotIn(other + " = ", symbol_path.read_text())
            source = project / "src" / (caller + ".c")
            source.parent.mkdir(exist_ok=True)
            source.write_text(f"extern int {callee}(int);\nint {caller}(int x) {{ return {callee}(x) + 1; }}\n")
            output = command("--project", str(project), "try", str(source))
            self.assertIn("us:", output)
            self.assertIn("eu:", output)
            self.assertIn("8/8", output)
            self.assertEqual(output.count("identical 8 of 8 instructions"), 2)
            target = project / "src" / (callee + ".c")
            target.write_text(
                f"int {callee}(int x) {{\n"
                "#if defined(VERSION_EU)\n return (x + 3) & 255;\n"
                "#else\n return x + 3;\n#endif\n}\n"
            )
            output = command("--project", str(project), "try", str(target))
            self.assertIn("owner fuzzy bar: PASS", output)
            self.assertIn("identical 2 of 2 instructions", output)
            self.assertIn("identical 3 of 3 instructions", output)
