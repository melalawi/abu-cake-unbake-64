"""Installed batch correspondence proves both cartridges and survives replans."""

import hashlib
import json
import os
import struct
import subprocess
import sys
import sysconfig
import tempfile
import unittest
from pathlib import Path

import toml

from tests.project.test_rom import cartridge


class SymbolJoinProofTests(unittest.TestCase):
    def test_batch_proof_refresh_replan_and_shared_c(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            project = directory / "Correspondence"
            values = toml.loads(Path(os.environ["UNBAKE_POLICY"]).read_text())
            values.update(symbol_similarity_threshold=0.9, symbol_similarity_margin=0.1)
            policy = directory / "policy.toml"
            policy.write_text(toml.dumps(values))
            environment = dict(os.environ, PYTHONNOUSERSITE="1", UNBAKE_POLICY=str(policy))
            environment.pop("PYTHONPATH", None)
            script = Path(sysconfig.get_path("scripts")) / "unbake"
            launcher = directory / "cli.py"
            launcher.write_text(
                "import runpy, sys, zlib\n"
                "from unbake.project import header\n"
                "header.RETAIL[zlib.crc32(bytes(0xfc0))] = '6102/7101'\n"
                f"sys.argv[0] = {str(script)!r}\n"
                f"runpy.run_path({str(script)!r}, run_name='__main__')\n"
            )

            def command(*arguments, expected=0):
                result = subprocess.run(
                    [sys.executable, str(launcher), *arguments],
                    env=environment,
                    cwd=directory,
                    capture_output=True,
                    text=True,
                    timeout=120,
                )
                self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
                self.assertNotIn("Traceback", result.stdout + result.stderr)
                return result.stdout + result.stderr

            command("init", str(project))
            for region, literal in (("E", 3), ("P", 5)):
                code = bytearray(0x180)
                boot = [0x3C088000, 0x25082180, 0x3C098000, 0x25292200, 0x25080004, 0x0109082B, 0x1420FFFD, 0xAD00FFFC]
                for at in (0x1100, 0x1120, 0x1140):
                    boot.extend([0x0C000000 | ((0x80000000 + at) >> 2 & 0x03FFFFFF), 0])
                boot.extend([0x03E00008, 0])
                struct.pack_into(f">{len(boot)}I", code, 0, *boot)
                struct.pack_into(">3I", code, 0x100, 0x3C028001, 0x03E00008, 0)
                for at, value in ((0x120, literal), (0x140, literal + 10)):
                    struct.pack_into(">2I", code, at, 0x03E00008, 0x24820000 | value)
                (project / "roms" / region).write_bytes(cartridge(region=region, seed=0x3C, instructions=bytes(code)))
            command("--project", str(project), "setup", "--names-from", "us", "--version-order", "us,eu", expected=1)
            layout = json.loads((project / "build/setup/layout.json").read_bytes())
            arguments = ["--project", str(project), "setup"]
            for name in layout["items"]:
                arguments.extend(["--compiler", name + "=gcc-2.7.2-kmc"])
            command(*arguments, expected=1)
            token = hashlib.sha256((project / "build/setup/proposal.json").read_bytes()).hexdigest()
            command("--project", str(project), "setup", "--confirm", token)
            path = project / "docs/setup/layout.json"
            layout = json.loads(path.read_bytes())
            ff = {v: {f["start"]: f for f in row["functions"]} for v, row in layout["versions"].items()}
            joins = []
            for at, name in ((0x1120, "first_join"), (0x1140, "second_join")):
                self.assertNotEqual(ff["us"][at]["name"], ff["eu"][at]["name"])
                placements = [
                    dict(version=v, start=at, end=ff[v][at]["end"], body_sha256=ff[v][at]["body_sha256"])
                    for v in ("us", "eu")
                ]
                joins.append(dict(name=name, placements=placements, evidence="Reviewed version-specific literals"))
            mapping = directory / "joins.json"
            mapping.write_text(json.dumps(joins))
            before = path.read_bytes()
            command("--project", str(project), "split", "join", "--map", str(mapping))
            self.assertEqual(before, path.read_bytes())
            source_root = project / "src"
            source_root.mkdir(exist_ok=True)
            affected = source_root / (ff["us"][0x1120]["name"] + ".c")
            affected.write_text("int affected(void) { return 0; }\n")
            output = command("--project", str(project), "split", "join", "--map", str(mapping), "--apply", expected=1)
            self.assertIn("split.join.authored_source", output)
            self.assertEqual(before, path.read_bytes())
            affected.unlink()
            # Real mismatching C in an unrelated item must stop the cartridge
            # proof without publishing any join or replacing a generation.
            configuration = toml.loads((project / "config.toml").read_text())
            changed_splits = {}
            for v in ("us", "eu"):
                split_path = project / configuration["version"][v]["split"]
                changed_splits[split_path] = split_path.read_text()
                anchor = ff[v][0x1100]["name"]
                split_path.write_text(
                    split_path.read_text().replace(', asm, "' + anchor + '"', ', c, "' + anchor + '"')
                )
            bad_source = source_root / (ff["us"][0x1100]["name"] + ".c")
            bad_source.write_text(f"int {bad_source.stem}(void) {{ return 17; }}\n")
            canonical = {
                p: p.read_bytes()
                for p in project.rglob("*")
                if p.is_file()
                and "build" not in p.relative_to(project).parts
                and "asm" not in p.relative_to(project).parts
            }
            generations = {v: (project / "build" / v).resolve() for v in ("us", "eu")}
            output = command("--project", str(project), "split", "join", "--map", str(mapping), "--apply", expected=1)
            self.assertIn("setup.sha1.", output)
            self.assertEqual(canonical, {p: p.read_bytes() for p in canonical})
            self.assertEqual(generations, {v: (project / "build" / v).resolve() for v in ("us", "eu")})
            bad_source.unlink()
            for split_path, text in changed_splits.items():
                split_path.write_text(text)
            output = command("--project", str(project), "split", "join", "--map", str(mapping), "--apply")
            self.assertEqual(output.count("every cartridge byte proved"), 2)
            joined = json.loads(path.read_bytes())
            self.assertEqual(len(joined["items"]), len(layout["items"]) - 2)
            self.assertEqual(len(joined["symbol_assertions"]), 2)
            for name in ("first_join", "second_join"):
                self.assertEqual(joined["items"][name]["versions"], ["us", "eu"])
                self.assertEqual(len(joined["items"][name]["body_groups"]), 2)
                for v in ("us", "eu"):
                    symbols = (project / "versions" / v / "symbol_addrs.txt").read_text()
                    self.assertEqual(symbols.count(name + " = "), 1)
            output = command("--project", str(project), "setup")
            self.assertEqual(output.count("every cartridge byte proved"), 2)
            self.assertEqual(joined["symbol_assertions"], json.loads(path.read_bytes())["symbol_assertions"])
            command("--project", str(project), "setup", "--replan-symbols")
            report = json.loads((project / "build/setup/symbol-proposal.json").read_bytes())
            self.assertEqual(report["replacements"], {})
            self.assertEqual(joined["symbol_assertions"], report["layout"]["symbol_assertions"])
            for name, extra in (("first_join", 0), ("second_join", 10)):
                source = project / "src" / (name + ".c")
                source.parent.mkdir(exist_ok=True)
                source.write_text(
                    f"int {name}(int x) {{\n#if defined(VERSION_EU)\nreturn x+{5 + extra};\n"
                    f"#else\nreturn x+{3 + extra};\n#endif\n}}\n"
                )
                output = command("--project", str(project), "try", str(source))
                self.assertEqual(output.count("identical 2 of 2 instructions"), 2)

            current = path.read_bytes()
            corrupt = json.loads(current)
            corrupt["symbol_assertions"][0]["placements"][0]["body_sha256"] = "0" * 64
            path.write_text(json.dumps(corrupt))
            output = command("--project", str(project), "setup", "--replan-symbols", expected=1)
            self.assertIn("setup.symbol_assertion_stale: first_join", output)
            path.write_bytes(current)
