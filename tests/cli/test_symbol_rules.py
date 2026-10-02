"""Installed retained-layout CLI tests for conservative symbol correspondence."""

import hashlib
import json
import os
import shutil
import struct
import subprocess
import sys
import sysconfig
import tempfile
import unittest
from pathlib import Path

import toml

from tests.project.test_rom import cartridge


def leaf(seed, changed=False):
    code = [0x24820000 | seed, *[0x30420000 | (seed + i) for i in range(1, 18)], 0x03E00008, 0]
    if changed:
        code[9] ^= 1
    return code


LEFT = [0x3C028001, 0x03E00008, 0]
RIGHT = [0x3C038002, 0x03E00008, 0]
INSERT = [0xAC830004, 0x03E00008, 0]


class SymbolRuleTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.project = self.directory / "Symbols"
        shutil.copytree(Path(__file__).parents[1] / "fixture", self.project)
        (self.project / "roms").mkdir(exist_ok=True)
        self.configuration = toml.loads((self.project / "config.toml").read_text())
        values = toml.loads(Path(os.environ["UNBAKE_POLICY"]).read_text())
        values.update(symbol_similarity_threshold=0.9, symbol_similarity_margin=0.1)
        self.policy = self.directory / "policy.toml"
        self.policy.write_text(toml.dumps(values))
        self.environment = dict(os.environ, PYTHONNOUSERSITE="1", UNBAKE_POLICY=str(self.policy))
        self.environment.pop("PYTHONPATH", None)
        script = Path(sysconfig.get_path("scripts")) / "unbake"
        self.launcher = self.directory / "cli.py"
        self.launcher.write_text(
            "import runpy, sys, zlib\n"
            "from unbake.project import header\n"
            "header.RETAIL[zlib.crc32(bytes(0xfc0))] = '6102/7101'\n"
            f"sys.argv[0] = {str(script)!r}\n"
            f"runpy.run_path({str(script)!r}, run_name='__main__')\n"
        )

    def inventory(self, source, target):
        layout = {
            "schema": 1,
            "project_id": self.configuration["project"]["id"],
            "workspace_id": self.configuration["workspace"]["id"],
            "names_from": "us",
            "rom_sha1": {},
            "versions": {},
            "items": {},
            "inputs_sha256": {},
        }
        for version, sequence in zip(("us", "us-rev1"), (source, target), strict=True):
            code = b"".join(struct.pack(f">{len(body)}I", *body) for body in sequence)
            image = cartridge(seed=0x3C, instructions=code)
            configured = self.configuration["version"][version]
            (self.project / configured["baserom"]).write_bytes(image)
            configured["baserom_sha1"] = hashlib.sha1(image).hexdigest()
            layout["rom_sha1"][version] = configured["baserom_sha1"]
            offset = 0x1000
            rows, symbols, functions = [], [], []
            for index, body in enumerate(sequence):
                name = f"unit_{version.replace('-', '_')}_{index}"
                address = 0x80000000 + offset
                rows.append(f"      - [0x{offset:X}, asm, {name}]\n")
                symbols.append(f"{name} = 0x{address:X};\n")
                functions.append(
                    dict(start=offset, end=offset + len(body) * 4, address=address, name=name, evidence={})
                )
                offset += len(body) * 4
            (self.project / configured["split"]).write_text(
                "options:\n  basename: fixture\nsegments:\n  - name: text\n    type: code\n"
                "    start: 0x1000\n    vram: 0x80001000\n    subsegments:\n" + "".join(rows) + f"  - [0x{offset:X}]\n"
            )
            (self.project / configured["symbols"]).write_text("".join(symbols))
            layout["versions"][version] = dict(functions=functions, providers=[], loaded_spans=[], evidence={})
        (self.project / "config.toml").write_text(toml.dumps(self.configuration))
        path = self.project / "docs/setup/layout.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(layout))

    def preview(self, *, seed=0, expected=0):
        result = subprocess.run(
            [sys.executable, str(self.launcher), "--project", str(self.project), "setup", "--replan-symbols"],
            env=dict(self.environment, PYTHONHASHSEED=str(seed)),
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
        if expected:
            return result.stdout + result.stderr
        return json.loads((self.project / "build/setup/symbol-proposal.json").read_text())

    @staticmethod
    def records(report, version):
        return report["layout"]["versions"][version]["functions"]

    def test_graph_free_leaf_requires_explicit_high_similarity(self):
        self.inventory([LEFT, leaf(100), RIGHT], [LEFT, leaf(100, True), RIGHT])
        report = self.preview()
        a, b = (self.records(report, v)[1] for v in ("us", "us-rev1"))
        self.assertEqual(a["name"], b["name"])
        detail = a["evidence"]["symbol_correspondence"]
        self.assertEqual(detail["reason"], "anchor-leaf-similarity")
        self.assertEqual(detail["callers"], [])
        self.assertEqual(detail["callees"], [])
        proof = detail["joins"][0]["evidence"][0]["similarity"]
        self.assertEqual(proof["score"], 0.95)
        self.assertEqual(report["similarity_distribution"]["leaf_accepted_bins"], {"0.9..<1": 1})
        self.assertEqual(proof["instruction_diff"][0]["operation"], "replace")
        values = toml.loads(self.policy.read_text())
        values["symbol_similarity_threshold"] = 0.99
        self.policy.write_text(toml.dumps(values))
        report = self.preview()
        a, b = (self.records(report, v)[1] for v in ("us", "us-rev1"))
        self.assertNotEqual(a["name"], b["name"])
        self.assertEqual(a["evidence"]["correspondence"], "symbol-leaf-similarity-low")
        for field in ("symbol_similarity_threshold", "symbol_similarity_margin"):
            absent = dict(values)
            del absent[field]
            self.policy.write_text(toml.dumps(absent))
            self.assertIn("policy." + field, self.preview(expected=1))

    def test_count_mismatch_aligns_changed_bodies_and_names_the_insertion(self):
        self.inventory([LEFT, leaf(100), leaf(500), RIGHT], [LEFT, INSERT, leaf(100, True), leaf(500, True), RIGHT])
        report = self.preview()
        a, b = self.records(report, "us"), self.records(report, "us-rev1")
        for i in (1, 2):
            self.assertEqual(a[i]["name"], b[i + 1]["name"])
            detail = a[i]["evidence"]["symbol_correspondence"]
            proof = detail["joins"][0]["evidence"][0]
            self.assertEqual(proof["position_rule"], "forced-sequence-alignment")
            self.assertEqual(proof["counts"], [2, 3])
        self.assertEqual(b[1]["evidence"]["correspondence"], "symbol-alignment-insertion-deletion")

    def test_fixpoint_rechecks_callers_after_alignment_supplies_an_anchor(self):
        # The changed caller is below the similarity bar. It becomes positionally
        # bounded after the leaf joins; its resolved callee is the right anchor.
        target = 0x80001000 + 4 * (len(LEFT) + len(leaf(100)) + 20)
        caller = [0x0C000000 | (target >> 2 & 0x03FFFFFF), 0, *leaf(500)[:-2], 0x03E00008, 0]
        target = 0x80001000 + 4 * (len(LEFT) + len(INSERT) + len(leaf(100)) + len(caller))
        changed = [0x0C000000 | (target >> 2 & 0x03FFFFFF), 0, *leaf(700)[:-2], 0x03E00008, 0]
        # The first target above uses the actual caller length, including the jal.
        caller[0] = 0x0C000000 | ((0x80001000 + 4 * (len(LEFT) + 20 + len(caller))) >> 2 & 0x03FFFFFF)
        self.inventory([LEFT, leaf(100), caller, RIGHT], [LEFT, INSERT, leaf(100, True), changed, RIGHT])
        report = self.preview()
        a, b = self.records(report, "us"), self.records(report, "us-rev1")
        self.assertEqual(a[1]["name"], b[2]["name"])
        self.assertEqual(a[2]["name"], b[3]["name"])
        detail = a[2]["evidence"]["symbol_correspondence"]
        self.assertEqual(detail["reason"], "anchor-call-graph")
        self.assertEqual(detail["joins"][0]["round"], 2)
        self.assertEqual(detail["fixpoint_rounds"], 2)
        self.assertLess(detail["joins"][0]["evidence"][0]["similarity"]["score"], 0.9)
        self.assertEqual(report, self.preview(seed=73))

    def test_wrong_join_trap_refuses_near_twins_and_crossing_alignment(self):
        first_twin, second_twin = leaf(100), leaf(100, True)
        first_twin[8] ^= 1
        second_twin[7] ^= 1
        for source, target in (
            ([LEFT, leaf(100), leaf(100, True), RIGHT], [LEFT, first_twin, second_twin, RIGHT]),
            ([LEFT, leaf(100), leaf(500), RIGHT], [LEFT, leaf(500, True), leaf(100, True), INSERT, RIGHT]),
        ):
            with self.subTest(target=target):
                self.inventory(source, target)
                report = self.preview()
                a, b = self.records(report, "us"), self.records(report, "us-rev1")
                self.assertNotEqual(a[1]["name"], b[2]["name"])
                self.assertIn("ambiguous", a[1]["evidence"]["correspondence"])
