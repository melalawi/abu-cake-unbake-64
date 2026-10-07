"""Real RW entry ABI reads do not traverse unrelated or other-version shard bodies."""

import gzip
import json
from pathlib import Path
from unittest.mock import patch

from tests.project_fixture import ProjectCase
from unbake import land, runner
from unbake.config import Held
from unbake.decomp.draft_abi import mapped_body
from unbake.objects.elf import Object
from unbake.typemap import shards

FIXTURES = Path(__file__).parents[1] / "compilers" / "fixtures" / "rw_helper_entry"
NAME = "func_802651B0_de"
NATIVE_INVENTORY = json.loads(gzip.decompress((FIXTURES / "map-inventory.json.gz").read_bytes()))
BODIES = json.loads(gzip.decompress((FIXTURES / "target-bodies.json.gz").read_bytes()))


class MappedEntryScopeTests(ProjectCase):
    versions = ("de", "eu", "eu-x", "us", "us-rev1")

    def setUp(self):
        super().setUp()
        self.inventory = json.loads(json.dumps(NATIVE_INVENTORY))
        for version in self.versions:
            payload = (FIXTURES / f"entry-{version}.bin").read_bytes()
            row = self.inventory[NAME]["versions"][version]
            row.update(start=0x40, end=0x40 + len(payload))
            configured = self.project.version(version)
            configured.baserom.write_bytes(bytes.fromhex("80371240") + bytes(0x3C) + payload)
            configured.split.write_text(
                f"segments:\n  - name: code\n    type: code\n    start: 0x40\n    vram: 0x{row['address']:X}\n"
                f"    subsegments:\n      - [0x40, asm, {NAME}]\n  - [0x{row['end']:X}]\n"
            )
            configured.symbols.write_text(f"{NAME} = 0x{row['address']:X}; // type:func\n")
        self.source = self.project.work / NAME / f"{NAME}.c"
        self.source.parent.mkdir(parents=True)
        self.source.write_bytes((FIXTURES / "candidate.c").read_bytes())

    def reader(self, containing):
        directory = self.root / f"shard-{len(list(self.root.glob('shard-*')))}"
        directory.mkdir()
        writer = shards.Writer(directory)
        try:
            for version in containing:
                writer.add(NAME, version, BODIES[version])
            path = writer.finish()
        finally:
            writer.close()
        return shards.Functions(path, self.inventory)

    def test_unrelated_missing_helper_does_not_block_requested_real_entry(self):
        functions = self.reader(self.versions)
        with (
            patch("unbake.typemap.mapping.load_map", return_value={"functions": functions}),
            patch.object(functions, "version", wraps=functions.version) as read,
        ):
            body = mapped_body(self.project, NAME, "de")
        self.assertEqual(body["target_sha256"], BODIES["de"]["target_sha256"])
        self.assertEqual(body["register_inputs"], BODIES["de"]["register_inputs"])
        self.assertEqual(read.call_args_list[0].args, (NAME, "de"))
        self.assertEqual(read.call_count, 1)

    def test_only_selected_version_is_required_and_no_complete_item_is_decoded(self):
        functions = self.reader(("de",))
        with (
            patch("unbake.typemap.mapping.load_map", return_value={"functions": functions}),
            patch.object(shards.Functions, "__getitem__", side_effect=AssertionError("complete item read")),
            patch.object(functions, "read", side_effect=AssertionError("batched unrelated read")),
        ):
            body = mapped_body(self.project, NAME, "de")
            self.assertEqual(body["calls"], BODIES["de"]["calls"])
            with self.assertRaisesRegex(Held, f"missing containing version for {NAME}: eu"):
                mapped_body(self.project, NAME, "eu")

    def test_all_holding_entry_checks_read_one_body_each(self):
        functions = self.reader(self.versions)
        with (
            patch("unbake.typemap.mapping.load_map", return_value={"functions": functions}),
            patch.object(functions, "version", wraps=functions.version) as read,
        ):
            for version in self.versions:
                self.assertEqual(
                    mapped_body(self.project, NAME, version)["target_sha256"], BODIES[version]["target_sha256"]
                )
        self.assertEqual([call.args for call in read.call_args_list], [(NAME, v) for v in self.versions])

    def test_real_alias_uses_inventory_and_one_selected_body(self):
        # Generic alternate alias metadata; actual code, canonical identity and ABI facts are unchanged.
        alias = "entry_alias"
        self.inventory[NAME]["aliases"].append(alias)
        configured = self.project.version("de")
        configured.symbols.write_text(
            configured.symbols.read_text() + f"{alias} = 0x{BODIES['de']['address']:X}; // type:func\n"
        )
        functions = self.reader(("de",))
        with (
            patch("unbake.typemap.mapping.load_map", return_value={"functions": functions}),
            patch.object(functions, "version", wraps=functions.version) as read,
        ):
            self.assertEqual(mapped_body(self.project, alias, "de")["target_sha256"], BODIES["de"]["target_sha256"])
        self.assertEqual(read.call_args.args, (NAME, "de"))
        self.assertEqual(read.call_count, 1)

    def test_requested_entry_absence_is_not_replaced_by_helper_contract_or_old_shard(self):
        functions = self.reader(())
        with patch("unbake.typemap.mapping.load_map", return_value={"functions": functions}):
            with self.assertRaisesRegex(Held, f"missing containing version for {NAME}: de"):
                mapped_body(self.project, NAME, "de")
            with self.assertRaisesRegex(Held, "missing containing version for __cmpdi2: de"):
                mapped_body(self.project, "__cmpdi2", "de")

    def test_current_rom_bytes_still_pin_requested_entry(self):
        functions = self.reader(("de",))
        rom = self.project.version("de").baserom
        changed = bytearray(rom.read_bytes())
        changed[0x40] ^= 1
        rom.write_bytes(changed)
        with (
            patch("unbake.typemap.mapping.load_map", return_value={"functions": functions}),
            self.assertRaisesRegex(Held, "types.abi.target_stale"),
        ):
            mapped_body(self.project, NAME, "de")

    def test_absent_identity_or_noncontaining_version_performs_no_body_read(self):
        functions = self.reader(())
        self.inventory[NAME]["versions"].pop("de")
        with (
            patch("unbake.typemap.mapping.load_map", return_value={"functions": functions}),
            patch.object(functions, "version", side_effect=AssertionError("unexpected body read")),
        ):
            self.assertIsNone(mapped_body(self.project, "unknown", "de"))
            self.assertIsNone(mapped_body(self.project, NAME, "de"))

    def test_plain_mapping_canonical_and_alias_forms_remain_supported(self):
        item = {
            "aliases": [NAME, "entry_alias"],
            "versions": {"de": {**BODIES["de"], **self.inventory[NAME]["versions"]["de"]}},
        }
        with patch("unbake.typemap.mapping.load_map", return_value={"functions": {NAME: item}}):
            self.assertEqual(mapped_body(self.project, NAME, "de")["calls"], BODIES["de"]["calls"])

    def test_fuzzy_admission_refuses_actual_missing_own_entry_before_compile_or_link(self):
        functions = self.reader(())
        expanded = (FIXTURES / "candidate.i").read_text()

        def compile_unit(*args, verify_input, **kwargs):
            verify_input(expanded)
            raise AssertionError("missing ABI must precede native compilation")

        with (
            patch("unbake.typemap.mapping.load_map", return_value={"functions": functions}),
            patch.object(runner, "compile_unit", side_effect=compile_unit) as compile_,
            patch.object(runner, "link_function", side_effect=AssertionError("no entry ABI")) as link,
        ):
            result, dependencies = land._fuzzy_builds_row(
                (self.project, self.project, self.host, NAME, self.source, "de")
            )
        self.assertFalse(result["compiled"])
        self.assertIsNone(result["percent"])
        self.assertFalse(result["exact"])
        self.assertIn(f"missing containing version for {NAME}: de", result["reason"])
        self.assertEqual(compile_.call_count, 1)
        self.assertEqual(link.call_count, 0)
        self.assertFalse(dependencies)

    def test_native_objects_separate_authored_decoder_call_from_inserted_runtime_call(self):
        candidate = Object(FIXTURES / "candidate.o")
        candidate_calls = [
            symbol["name"] for _, kind, symbol in candidate.relocations(candidate.section(".text")) if kind == 4
        ]
        self.assertEqual(candidate_calls, ["func_80264F60_de"])
        self.assertNotIn("__cmpdi2", runner.undefined(FIXTURES / "candidate.o"))
        helper_call = Object(FIXTURES / "generated-runtime-call.o")
        references = [
            symbol["name"] for _, kind, symbol in helper_call.relocations(helper_call.section(".text")) if kind == 4
        ]
        self.assertEqual(references, ["__floatdidf"])
        self.assertNotIn("__floatdidf", (FIXTURES / "generated-runtime-call.c").read_text())
        self.assertFalse(runner.undefined(FIXTURES / "generated-compare-call.o"))
        native = Object(FIXTURES / "native-compare-helper.o")
        body = native.content(native.section(".text"))
        for version in self.versions:
            self.assertEqual(body, (FIXTURES / f"helper-{version}.bin").read_bytes())
        self.assertEqual(len(body), 72)
