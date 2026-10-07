"""Four waiting owner queues at the existing native binary and Git boundaries."""

import json
import os
from pathlib import Path
from unittest.mock import patch

from tests.project_fixture import ProjectCase
from unbake import process
from unbake.config import Held
from unbake.project import publication_push

CASES = json.loads((Path(__file__).parent / "fixtures/minimal_publication_cases.json").read_text())


class MinimalPublicationTests(ProjectCase):
    versions = ("de", "eu", "eu-x", "us", "us-rev1")

    def case(self, lane):
        packet = next(row for row in CASES if row["lane"] == lane)
        changed = set()
        for entry in packet["sources"]:
            source = self.project.src / (entry["function"] + ".c")
            source.write_text(entry["source"])
            changed.add(source.relative_to(self.project.root).as_posix())
        expected = set()
        for version in self.versions:
            rom = bytearray(bytes.fromhex("80371240") + bytes(0x3C))
            text = "name: fixture\nsegments:\n  - [0x0, header, header]\n"
            symbols = ""
            for entry in packet["sources"]:
                unit = entry["function"]
                row = next((row for row in entry["rows"] if row["version"] == version), None)
                physical = row or entry["rows"][0]
                words = bytes.fromhex(physical["words"])
                start = len(rom)
                rom.extend(words)
                text += (
                    f"  - name: main\n    type: code\n    start: 0x{start:X}\n"
                    f"    vram: 0x{physical['address']:X}\n    subalign: 4\n    subsegments:\n"
                    f"      - [0x{start:X}, {'c' if row else 'asm'}, {unit}]\n"
                )
                symbols += f"{unit} = 0x{physical['address']:X};\n"
                if row:
                    # The external native build's final linked output boundary.
                    native = self.project.build_link(version) / "units" / (unit + ".bin")
                    native.parent.mkdir(parents=True, exist_ok=True)
                    native.write_bytes(words)
                    expected.add((unit, version))
            text += f"  - [0x{len(rom):X}]\n"
            meta = self.project.version(version)
            meta.baserom.write_bytes(rom)
            meta.split.write_text(text)
            meta.symbols.write_text(symbols)
        calls = []
        head, base = "a" * 40, "b" * 40

        def external(argv, cwd, phase, **kwargs):
            self.assertEqual(argv[0], "git")
            args = tuple(argv[1:])
            calls.append(args)
            if args == ("rev-parse", "HEAD"):
                return head
            if args == ("rev-parse", "FETCH_HEAD") or args[0] == "merge-base":
                return base
            if args[:2] == ("diff", "--name-only"):
                return "\0".join(sorted(changed))
            return ""

        with patch.object(process, "run_tool", side_effect=external):
            result = publication_push.push(self.project, self.host, "origin")
        measured = result["check"]
        self.assertEqual({(row["function"], row["version"]) for row in measured["scopes"]}, expected)
        self.assertEqual(measured["work"]["functions_compared"], len(expected))
        self.assertEqual(measured["work"]["native_bytes_read"], measured["work"]["rom_bytes_read"])
        self.assertEqual(
            [call for call in calls if call[0] == "push"], [("push", "--", "origin", head + ":refs/heads/main")]
        )
        if receipt := os.environ.get("MINIMAL_CASE_RECEIPTS"):
            with Path(receipt).open("a") as output:
                output.write(
                    json.dumps(
                        {
                            "case_id": self.id(),
                            "lane": lane,
                            "source_count": len(changed),
                            "holder_count": len(expected),
                            "elapsed_ns": measured["elapsed_ns"],
                            "work": measured["work"],
                        }
                    )
                    + "\n"
                )
        first = next(row for row in packet["sources"] if row["rows"])
        version = first["rows"][0]["version"]
        native = self.project.build_link(version) / "units" / (first["function"] + ".bin")
        original = native.read_bytes()
        native.write_bytes(bytes([original[0] ^ 1]) + original[1:])
        calls.clear()
        with patch.object(process, "run_tool", side_effect=external), self.assertRaises(Held) as held:
            publication_push.push(self.project, self.host, "origin")
        self.assertEqual(held.exception.key, "publish.native_mismatch")
        self.assertFalse(any(call[0] == "push" for call in calls))
        native.write_bytes(original)
        source = self.project.src / (first["function"] + ".c")
        source.write_text(source.read_text() + '\nvoid bad(void) { __asm__("nop"); }\n')
        calls.clear()
        with patch.object(process, "run_tool", side_effect=external), self.assertRaises(Held) as held:
            publication_push.push(self.project, self.host, "origin")
        self.assertEqual(held.exception.key, "publish.push_rules")
        self.assertFalse(any(call[0] == "push" for call in calls))

    def test_a5_changed_native_bytes_and_source_hygiene(self):
        self.case("rw-land-a5-sol-20261007")

    def test_legacy12_whole_atomic_native_bytes_and_source_hygiene(self):
        self.case("legacy-bulk12-20261007")

    def test_b4_changed_native_bytes_and_source_hygiene(self):
        self.case("rw-land-b4-sol-20261007")

    def test_pfs_changed_native_bytes_and_source_hygiene(self):
        self.case("rw-clean2-sol-20261007")
