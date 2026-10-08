"""Existing YAML source DATA rows using the actual 33,253-byte SN64 record cases."""

import gzip
import hashlib
import json
import os
import re
from pathlib import Path
from unittest.mock import patch

from tests.project_fixture import ProjectCase
from unbake import buildfiles
from unbake.config import Held
from unbake.layout import split
from unbake.project import publication_push

FIXTURE = Path(__file__).parent / "fixtures/source_data"
RECORDS = json.loads((FIXTURE / "manifest.json").read_text())


class SourceDataBuildTests(ProjectCase):
    versions = ("us",)

    def setUp(self):
        super().setUp()
        for row in RECORDS:
            name = row["symbol"]
            raw = gzip.decompress((FIXTURE / (name + ".c.gz")).read_bytes())
            self.assertEqual(hashlib.sha256(raw).hexdigest(), row["source_sha256"])
            (self.project.src / (name + ".c")).write_bytes(raw)
            native = self.project.build_link("us") / "data" / (name + ".bin")
            native.parent.mkdir(parents=True, exist_ok=True)
            raw = gzip.decompress((FIXTURE / (name + ".bin.gz")).read_bytes())
            self.assertEqual(hashlib.sha256(raw).hexdigest(), row["binary_sha256"])
            self.assertEqual(len(raw), row["bytes"])
            native.write_bytes(raw)
        (self.project.include[-1] / "sn64_type_records.h").write_bytes((FIXTURE / "sn64_type_records.h").read_bytes())
        meta = self.project.version("us")
        self.before = (
            "name: fixture\nsegments:\n  - [0x0, header, header]\n"
            "  - name: main\n    type: code\n    start: 0xC7568\n    vram: 0x800C6968\n"
            "    subsegments:\n      - [0xC7568, data, unclaimed]\n  - [0x16E000]\n"
        )
        rows = "      - [0xC7568, data, unclaimed]\n"
        for row in RECORDS:
            rows += f'      - [0x{row["rom_start"]:X}, data, "src/{row["symbol"]}.c"]\n'
            rows += f"      - [0x{row['rom_end']:X}, data, raw_tail_{row['rom_end']:X}]\n"
        meta.split.write_text(self.before.replace("      - [0xC7568, data, unclaimed]\n", rows))
        rom = bytearray(0x16E000)
        for row in RECORDS:
            native = self.project.build_link("us") / "data" / (row["symbol"] + ".bin")
            rom[row["rom_start"] : row["rom_end"]] = native.read_bytes()
        meta.baserom.write_bytes(rom)
        # The producer recipe is read back from the generated Makefile, not the fixture placeholder.
        make = self.project.root / "Makefile"
        make.write_text(buildfiles.makefile(self.project, self.host))
        for producer in (make, self.project.include[-1] / "sn64_type_records.h"):
            os.utime(producer, ns=(1, 1))  # producing inputs predate the accepted native bytes
        self.changed = {meta.split.relative_to(self.project.root).as_posix()}
        self.calls = []

    def git(self, project, *args):
        self.calls.append(args)
        if args[:2] == ("diff", "--name-only"):
            return "\0".join(sorted(self.changed))
        if args[0] == "show":
            return self.before
        return ""

    def admit(self):
        with patch.object(publication_push, "_git", side_effect=self.git):
            return publication_push.admission(self.project, self.host, "head", "base")

    def test_actual_odd_record_rows_replace_only_claimed_raw_bytes_in_generated_pieces(self):
        units = buildfiles.units(self.project, "us")
        self.assertEqual(
            [(u.start, u.address, u.size) for u in units], [(r["rom_start"], r["vram"], r["bytes"]) for r in RECORDS]
        )
        self.assertEqual(split.functions(self.project, "us"), [])
        text = buildfiles.slices_mk(self.project, "us")
        raw = [(int(a), int(b)) for a, b in re.findall(r"^us\.S\.\w+ := (\d+) (\d+)$", text, re.M)]
        self.assertEqual(raw, [(0, 0xF8E03), (0xFDDF0, 24), (0x101000, 0x16E000 - 0x101000)])
        self.assertEqual(sum(size for _, size in raw) + sum(u.size for u in units), 0x16E000)
        for unit in units:
            self.assertIn(f"build/us/data/{unit.name}.bin", text)
            self.assertIn(f"us.D.{unit.name} := 0x{unit.address:X}", text)
        self.assertEqual(text, buildfiles.slices_mk(self.project, "us"))

    def test_actual_metadata_only_registration_checks_33253_native_bytes_and_source_hygiene(self):
        with patch.object(publication_push.split, "words", side_effect=AssertionError("DATA is byte bounded")):
            result = self.admit()
        self.assertEqual(
            result["work"],
            {
                "native_bytes_read": 33253,
                "rom_bytes_read": 33253,
                "functions_compared": 0,
                "data_extents_compared": 2,
            },
        )
        self.assertEqual(
            [(r["data"], r["bytes"]) for r in result["scopes"]], [(r["symbol"], r["bytes"]) for r in RECORDS]
        )
        self.assertEqual(len([call for call in self.calls if call[0] == "show"]), 1)
        self.before = self.project.version("us").split.read_text()
        self.assertEqual(self.admit()["scopes"], [])
        self.changed = {"src/" + RECORDS[0]["symbol"] + ".c"}
        self.assertEqual(len(self.admit()["scopes"]), 1)

    def test_actual_metadata_only_registration_refuses_missing_mismatch_truncation_and_source_rules(self):
        native = self.project.build_link("us") / "data" / (RECORDS[0]["symbol"] + ".bin")
        original = native.read_bytes()
        for payload in (original[:-1], original + b"\0", bytes([original[0] ^ 1]) + original[1:]):
            native.write_bytes(payload)
            with self.assertRaises(Held) as held:
                self.admit()
            self.assertEqual(held.exception.key, "publish.native_mismatch")
        native.unlink()
        with self.assertRaises(Held) as held:
            self.admit()
        self.assertEqual(held.exception.key, "publish.native_missing")
        self.assertIn("data/" + native.name, held.exception.reason)
        native.write_bytes(original)
        source = self.project.src / (RECORDS[0]["symbol"] + ".c")
        original_source = source.read_text()
        for forbidden in ('void bad(void) { __asm__("nop"); }', "volatile int bad_storage = 1;"):
            source.write_text(original_source + "\n" + forbidden + "\n")
            with self.assertRaises(Held) as held:
                self.admit()
            self.assertEqual(held.exception.key, "publish.push_rules")
        source.unlink()
        with self.assertRaises(Held) as held:
            buildfiles.slices_mk(self.project, "us")
        self.assertEqual(held.exception.key, "buildfiles.source")

    def test_actual_source_binding_rebounds_select_even_unchanged_sources(self):
        self.before = self.project.version("us").split.read_text().replace("0xFDDF0,", "0xFDDEF,")
        self.assertEqual([r["data"] for r in self.admit()["scopes"]], [RECORDS[0]["symbol"]])
        self.before = self.project.version("us").split.read_text().replace('data, "src/', 'rodata, "src/', 1)
        self.assertEqual([r["data"] for r in self.admit()["scopes"]], [RECORDS[0]["symbol"]])

    def test_native_data_target_reuses_compiler_cas_and_copies_before_flags_with_exact_size(self):
        text = buildfiles.makefile(self.project, self.host)
        self.assertIn("build/$1/data/%.bin: build/$1/src/%.key versions/$1/slices.mk", text)
        self.assertIn("versions/$1/$$(NAME).data.ld | build/$1/data", text)
        recipe = text[text.index("DATA_BIN =") : text.index("SLICE =")]
        self.assertLess(recipe.index("cp build/cas/"), recipe.index("--set-section-flags"))
        self.assertIn("--section-start=.data=$(firstword $(subst :, ,$($(VER).D.$(*F))))", recipe)
        self.assertIn("--only-section=.data", recipe)
        self.assertIn("wc -c", recipe)
        self.assertNotIn("BASEROM", recipe)
        self.assertNotIn("N64LINK", recipe)
        script = buildfiles.data_link_script(self.project, "us")
        self.assertIn("*(.rdata .rdata.* .rodata", script)
        self.assertIn("INCLUDE versions/us/symbols.ld", script)
        self.assertIn("/DISCARD/ : { *(*) }", script)

    def test_same_real_source_can_own_code_and_data_but_duplicate_data_targets_fail(self):
        from types import SimpleNamespace

        row = RECORDS[0]
        code = SimpleNamespace(kind="c", path=row["symbol"], address=0x80001000, start=0x40, end=0x4C)
        with patch.object(buildfiles.split, "functions", return_value=[code]):
            text = buildfiles.slices_mk(self.project, "us")
        self.assertIn(f"us.U.{row['symbol']} := 0x80001000:0x40:0xC", text)
        self.assertIn(f"us.D.{row['symbol']} := 0x800F8203", text)
        meta = self.project.version("us")
        meta.split.write_text(meta.split.read_text().replace("data, raw_tail_FDDF0", f'data, "src/{row["symbol"]}.c"'))
        with self.assertRaises(Held) as held:
            buildfiles.units(self.project, "us")
        self.assertEqual(held.exception.key, "buildfiles.source")

    def concurrent_data_push(self, *, bad_native=False, bad_source=False):
        """Actual record rows reproduce the dictionaries owner's units.mk conflict."""
        from unbake import process

        packet = json.loads((FIXTURE / "generated_rebase.json").read_text())
        meta = self.project.version("us")
        merged = meta.split.read_text()
        remote = merged.replace(f'data, "src/{RECORDS[1]["symbol"]}.c"', "data, remote_raw")
        local = merged.replace(f'data, "src/{RECORDS[0]["symbol"]}.c"', "data, local_raw")
        meta.split.write_text(local)
        native = self.project.build_link("us") / "data" / (RECORDS[1]["symbol"] + ".bin")
        if bad_native:
            native.write_bytes(native.read_bytes()[:-1])
        if bad_source:
            source = self.project.src / (RECORDS[1]["symbol"] + ".c")
            source.write_text(source.read_text() + "\nvolatile int forbidden = 1;\n")
        sources = {p: p.read_bytes() for p in self.project.src.glob("*.c")}
        calls = []
        rebased = False
        for name in packet["conflicts"]:
            (self.project.root / name).write_text("<<<<<<< local\n=======\n>>>>>>> remote\n")

        def external(argv, cwd, phase, **kwargs):
            nonlocal rebased
            if argv[0] == str(self.host.n64link):
                self.assertEqual(argv[1:], ["--version"])
                return buildfiles.N64LINK_RELEASE
            self.assertEqual(argv[0], "git")
            args = tuple(argv[1:])
            calls.append(args)
            if args == ("rev-parse", "HEAD"):
                return "rebased" if rebased else "local"
            if args == ("rev-parse", "FETCH_HEAD"):
                return "remote"
            if args[0] == "merge-base":
                return "base"
            if args == ("rebase", "FETCH_HEAD"):
                meta.split.write_text(merged)
                raise Held(process.named("publish.git", packet["reason"], owner="process", stage="publish"))
            if args[:3] == ("diff", "--name-only", "--diff-filter=U"):
                return "\0".join(packet["conflicts"])
            if args == ("-c", "core.editor=true", "rebase", "--continue"):
                self.assertEqual((self.project.root / "units.mk").read_text(), buildfiles.units_mk(self.project))
                slices = self.project.root / "versions/us/slices.mk"
                self.assertEqual(slices.read_text(), buildfiles.slices_mk(self.project, "us"))
                for row in RECORDS:
                    self.assertIn("build/us/data/" + row["symbol"] + ".bin", slices.read_text())
                self.assertEqual(meta.split.read_text(), merged)
                self.assertEqual({p: p.read_bytes() for p in sources}, sources)
                rebased = True
                return ""
            if args == ("diff", "--name-only", "-z", "remote", "rebased"):
                return meta.split.relative_to(self.project.root).as_posix()
            if args[0] == "show":
                return remote
            return ""

        with (
            patch.object(process, "run_tool", side_effect=external),
            patch("unbake.report.progress.write", side_effect=AssertionError("no report gate for build conflicts")),
            patch("unbake.report.verify.validate", side_effect=AssertionError("no report gate for build conflicts")),
        ):
            if bad_native or bad_source:
                with self.assertRaises(Held) as held:
                    publication_push.push(self.project, self.host, "origin")
                self.assertEqual(held.exception.key, "publish.push_rules" if bad_source else "publish.native_mismatch")
                self.assertFalse(any(call[0] == "push" for call in calls))
            else:
                result = publication_push.push(self.project, self.host, "origin")
                self.assertEqual([r["data"] for r in result["check"]["scopes"]], [RECORDS[1]["symbol"]])
                self.assertEqual(result["check"]["work"]["native_bytes_read"], RECORDS[1]["bytes"])
                self.assertEqual(
                    [c for c in calls if c[0] == "push"], [("push", "--", "origin", "rebased:refs/heads/main")]
                )
        self.assertEqual(calls.count(("-c", "core.editor=true", "rebase", "--continue")), 1)
        self.assertFalse(any(call[0] == "show" and call[1].startswith(":") for call in calls))

    def test_actual_concurrent_data_generated_conflicts_regenerate_merged_rows_before_push(self):
        self.concurrent_data_push()

    def test_actual_concurrent_data_rebase_still_refuses_native_mismatch(self):
        self.concurrent_data_push(bad_native=True)

    def test_actual_concurrent_data_rebase_still_refuses_source_hygiene(self):
        self.concurrent_data_push(bad_source=True)

    def test_generated_build_conflicts_refuse_authored_paths_without_writes(self):
        for authored in (
            "src/alpha.c",
            "versions/us/game.yaml",
            "config.toml",
            "versions/us/custom.ld",
            "README.md",
            "versions/us/report.json",
            "report-state.json",
        ):
            with (
                self.subTest(authored=authored),
                patch.object(publication_push, "_git", return_value="units.mk\0" + authored),
                patch.object(buildfiles, "write", side_effect=AssertionError("authored conflict must refuse")),
            ):
                self.assertFalse(publication_push.resolve_conflicts(self.project, self.host))

    def test_each_generated_native_build_projection_uses_current_canonical_generator(self):
        for name in ("Makefile", "units.mk", "versions/us/slices.mk", "versions/us/symbols.ld"):
            with (
                self.subTest(name=name),
                patch.object(publication_push, "_git", side_effect=[name, ""]),
                patch.object(buildfiles, "write", return_value=[self.project.root / name]) as write,
                patch("unbake.report.progress.write", side_effect=AssertionError("no report work")),
                patch("unbake.report.verify.validate", side_effect=AssertionError("no report gate")),
            ):
                self.assertTrue(publication_push.resolve_conflicts(self.project, self.host))
                self.assertEqual(write.call_args.args, (self.project, self.host))
