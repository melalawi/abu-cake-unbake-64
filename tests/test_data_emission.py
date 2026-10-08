"""Replay actual DATA2 source/native ownership failures; only external processes are mocked."""

import hashlib
import json
import shutil
import struct
from pathlib import Path
from unittest.mock import patch

import toml

from tests.elf_fixture import write_object
from tests.project_fixture import ProjectCase
from unbake import config, process, runner
from unbake.config import Held
from unbake.layout import split
from unbake.objects import rodata
from unbake.objects.elf import Object
from unbake.project.headers import Graph
from unbake.report import data
from unbake.work import attempts

FIXTURE = Path(__file__).parent / "fixtures/data_emission"
NAME = "controller_pak_menu_reload_seconds"
VALUE = bytes.fromhex("41900000")


class DataEmissionTests(ProjectCase):
    versions = ("us",)

    def setUp(self):
        super().setUp()
        self.source = self.project.src / "func_80444CF8_de.c"
        self.source.write_bytes((FIXTURE / "named.c").read_bytes())
        (self.project.include[0] / "types.h").write_text(
            "typedef int s32; typedef float f32; typedef unsigned char u8;\n"
        )
        provider = self.project.include[0] / "span_16E000/code_80444030.h"
        provider.parent.mkdir()
        provider.write_text("/* Actual candidate's owning include; typedefs from types.h. */\n")
        self.start = 0x64
        self.address = 0x80001024
        self.rom(VALUE + bytes.fromhex("00000014"))
        for name in ("Makefile", "units.mk", "tools/n64link.version"):
            (self.project.root / name).write_text("native fixture recipe\n")
        for name in ("fixture.ld", "symbols.ld"):
            (self.project.root / "versions/us" / name).write_text(
                "SECTIONS\n{\n  .text : { *(.text) }\n  /DISCARD/ : { *(*) }\n}\n" if name == "fixture.ld" else ""
            )
        self.original = self.root / "func_80444CF8_de.o"
        self.original.write_bytes((FIXTURE / "named-original.elf32").read_bytes())
        self.placed = self.root / "placed.o"
        self.final = self.root / "final.elf"
        self.calls = []

    def rom(self, content):
        meta = self.project.version("us")
        meta.baserom.write_bytes(meta.baserom.read_bytes()[: self.start] + content)
        yaml = meta.split.read_text()
        if "rodata, constants" not in yaml:
            yaml = yaml.replace("  - [0x64]", "      - [0x64, rodata, constants]\n  - [0x6C]")
        meta.split.write_text(yaml)
        values = toml.load(self.project.root / "config.toml")
        values["version"]["us"]["baserom_sha1"] = hashlib.sha1(meta.baserom.read_bytes()).hexdigest()
        (self.project.root / "config.toml").write_text(toml.dumps(values))
        self.project = config.load(self.project.root)

    def place(self):
        """Mock n64link's symbol rewrite, retaining the actual compiler object tables."""
        obj = Object(self.original)
        section = obj.section(".rdata")
        for table, symbols in obj.symbols.items():
            for symbol in symbols:
                if symbol["section"] == section:
                    at = obj.sections[table][4] + 16 * symbol["index"]
                    struct.pack_into(">I", obj.data, at + 4, self.address + symbol["value"])
                    struct.pack_into(">H", obj.data, at + 14, 0xFFF1)
        self.placed.write_bytes(obj.data)
        sizes = {}
        Graph.capture(self.project).initialized_definitions(self.project, self.source, "us", sizes=sizes)
        return rodata.initialized_sections(
            Object(self.original), Object(self.placed), ((self.address, self.start, 8),), definition_sizes=sizes
        )

    def capture(self):
        deps = data.capture_inputs(
            self.project,
            self.source,
            "us",
            [str(self.host.cpp), "-I", str(self.project.include[0])],
            compiler=runner._compiler_pins(self.project, self.source.stem),
            non_matching=False,
        ).document()
        deps["values"].update(
            object_sha256=hashlib.sha256(self.original.read_bytes()).hexdigest(), link_tools=data.link_tools(self.host)
        )
        self.original.with_suffix(".inputs.json").write_text(json.dumps(deps))

    def linked(self, layout, *, content=None, executable=True):
        emitted = Object(self.original).content(Object(self.original).section(".rdata")) if content is None else content
        item = layout[0]
        write_object(
            self.final,
            {item.output_name: emitted},
            [(NAME, item.output_name, item.address, 4, 0x11)],
            addresses={item.output_name: item.address},
            linked=executable,
        )

    def coverage(self):
        source_hash = hashlib.sha256(self.source.read_bytes()).hexdigest()
        return data.coverage(
            self.project,
            "us",
            {self.source.stem: (source_hash, set(), False)},
            attempts.ledger(self.project).latest("native.data", "us"),
        )

    def test_actual_sn64_zero_size_named_label_binds_only_its_scalar_not_anonymous_duplicate(self):
        original = Object(self.original)
        labels = {symbol["name"]: symbol for table in original.symbols.values() for symbol in table}
        self.assertEqual((labels[NAME]["info"], labels[NAME]["size"], labels[NAME]["value"]), (0, 0, 0))
        self.assertEqual(original.content(original.section(".rdata")), VALUE + VALUE)
        self.assertEqual(labels["RODATA_SYM_0"]["value"], 4)
        layout = self.place()
        self.assertEqual(layout[0].symbols, ((NAME, 0, 4),))
        self.assertEqual((layout[0].address, layout[0].rom_offset, layout[0].size), (self.address, self.start, 8))
        self.capture()
        self.linked(layout)
        event = data.record_linked(self.project, self.host, self.source, "us", self.original, self.final, layout)
        self.assertIsNotNone(event)
        self.assertEqual(self.coverage().manifest["verified_bytes"], 4)
        self.assertEqual(self.coverage().manifest["unmeasured_bytes"], 4)

    def test_actual_retained_data1_named_string_original_placed_and_final_native_elf_produce_nine_bytes(self):
        fixture = FIXTURE / "data1"
        self.source = self.project.src / "func_80433EA0_de.c"
        self.source.write_bytes((fixture / "source.c").read_bytes())
        shutil.copytree(fixture / "include", self.project.include[0], dirs_exist_ok=True)
        self.original.write_bytes((fixture / "original.elf32").read_bytes())
        self.placed.write_bytes((fixture / "placed.elf32").read_bytes())
        self.final.write_bytes((fixture / "final.elf32").read_bytes())
        expected = (fixture / "linked-string.bin").read_bytes()
        self.assertEqual(expected, b"%d.%s.%s\0")
        self.rom(expected)
        meta = self.project.version("us")
        meta.split.write_text(meta.split.read_text().replace("0x80001000", "0x800E1EC0").replace("0x6C", "0x6D"))
        sizes = {}
        definitions = Graph.capture(self.project).initialized_definitions(self.project, self.source, "us", sizes=sizes)
        name = "controller_pak_note_label_format"
        self.assertEqual(sizes[name], 9)
        self.assertIn(name, definitions)
        layout = rodata.initialized_sections(
            Object(self.original), Object(self.placed), ((0x800E1EE4, self.start, 9),), definition_sizes=sizes
        )
        self.assertEqual(layout[0].symbols, ((name, 0, 9),))
        self.capture()
        self.assertIsNotNone(
            data.record_linked(self.project, self.host, self.source, "us", self.original, self.final, layout)
        )
        self.assertEqual(self.coverage().manifest["verified_bytes"], 9)

    def test_actual_data2_named_duplicate_at_native_section_base_mismatches_and_has_zero_credit(self):
        # Actual E33BC..E33C4 bytes: named duplicate would precede the loaded pool.
        self.rom(bytes.fromhex("0044434C") + VALUE)
        layout = self.place()
        self.capture()
        self.linked(layout)
        self.assertIsNone(
            data.record_linked(self.project, self.host, self.source, "us", self.original, self.final, layout)
        )
        self.assertEqual(self.coverage().manifest["verified_bytes"], 0)

    def test_actual_complete_section_cannot_be_bound_to_only_the_four_byte_pilot_window(self):
        self.place()
        with self.assertRaisesRegex(Held, "complete ROM mapping"):
            rodata.initialized_sections(
                Object(self.original),
                Object(self.placed),
                ((self.address, self.start, 4),),
                definition_sizes={NAME: 4},
            )

    def test_runner_emits_native_resident_section_and_records_final_link_without_changing_version_script(self):
        layout = self.place()
        self.capture()
        script = self.project.root / "versions/us/fixture.ld"
        before = script.read_bytes()
        row = split.functions(self.project, "us")[0]
        expected = split.words(self.project, row)
        (script.parent / "symbols.ld").write_text(
            "".join(
                f"PROVIDE({name} = 0x80001000);\n"
                for name in ("D_8014DE70", "func_8040C2F8_de", "D_800DE88B", "D_80142788")
            )
        )
        self.capture()

        def native(argv, work, phase, **kwargs):
            self.calls.append(argv)
            if "-T" in argv:
                emitted_script = Path(argv[argv.index("-T") + 1]).read_text()
                self.assertIn(layout[0].output_name, emitted_script)
                self.assertLess(emitted_script.index(layout[0].output_name), emitted_script.index("/DISCARD/"))
                write_object(
                    Path(argv[argv.index("-o") + 1]),
                    {".text": expected, layout[0].output_name: VALUE + VALUE},
                    addresses={".text": row.address, layout[0].output_name: self.address},
                    linked=True,
                )
            elif "-O" in argv:
                Path(argv[-1]).write_bytes(expected)
            else:
                self.assertIn("--set-section-flags", argv)
            return ""

        with patch.object(process, "run_tool", side_effect=native):
            result = runner.link(self.project, self.host, self.placed, "us", row, self.root, self.source, self.original)
        self.assertEqual(result, expected)
        self.assertEqual(script.read_bytes(), before)
        self.assertEqual(self.coverage().manifest["verified_bytes"], 4)

    def test_actual_unnamed_retained_source_candidate_and_volatile_source_never_supply_named_authority(self):
        layout = self.place()
        self.linked(layout)
        for contents in (
            (FIXTURE / "unnamed.c").read_text(),
            (FIXTURE / "named.c").read_text().replace("const f32", "const volatile f32"),
        ):
            self.source.write_text(contents)
            self.capture()
            self.assertEqual(Graph.capture(self.project).initialized_definitions(self.project, self.source, "us"), {})
            self.assertIsNone(
                data.record_linked(self.project, self.host, self.source, "us", self.original, self.final, layout)
            )
        candidate = self.project.work / "candidate.c"
        candidate.parent.mkdir(exist_ok=True)
        candidate.write_bytes((FIXTURE / "named.c").read_bytes())
        self.assertIsNone(
            data.record_linked(self.project, self.host, candidate, "us", self.original, self.final, layout)
        )

    def test_unrelocated_object_or_missing_final_named_section_cannot_be_final_proof(self):
        layout = self.place()
        self.capture()
        self.linked(layout, executable=False)
        with self.assertRaisesRegex(Held, "final relocated executable"):
            data.record_linked(self.project, self.host, self.source, "us", self.original, self.final, layout)
        write_object(self.final, {".text": bytes(4)}, linked=True)
        with self.assertRaisesRegex(Held, "final initialized section missing"):
            data.record_linked(self.project, self.host, self.source, "us", self.original, self.final, layout)

    def test_provider_or_object_changed_after_compile_invalidates_native_producer(self):
        layout = self.place()
        self.capture()
        self.linked(layout)
        provider = self.project.include[0] / "types.h"
        before = provider.read_bytes()
        provider.write_bytes(before + b"\n")
        self.assertIsNone(
            data.record_linked(self.project, self.host, self.source, "us", self.original, self.final, layout)
        )
        provider.write_bytes(before)
        self.original.write_bytes(self.original.read_bytes() + b"\0")
        self.assertIsNone(
            data.record_linked(self.project, self.host, self.source, "us", self.original, self.final, layout)
        )

    def test_source_and_missing_earlier_header_probe_races_are_refused_at_compile_boundary(self):
        for mutation in ("source", "earlier-header"):

            def native(argv, work, phase, mutation=mutation, **kwargs):
                (work / (self.source.stem + ".o")).write_bytes(self.original.read_bytes())
                if mutation == "source":
                    self.source.write_bytes(self.source.read_bytes() + b"\n")
                else:
                    (self.project.src / "types.h").write_text("typedef double f32;\n")
                return ""

            with (
                patch.object(process, "run_tool", side_effect=native),
                patch("unbake.compilers.drivers.run_preprocess", return_value=self.source.read_text()),
                self.assertRaisesRegex(Held, "changed during native production"),
                runner.compile_unit(self.project, self.host, self.source, "us", unit=self.source.stem),
            ):
                self.fail("changed native inputs reached a consumer")

    def test_final_relocated_pointer_bytes_supply_credit_raw_initializer_bytes_do_not(self):
        self.source.write_text(
            "extern int target; const unsigned int controller_pak_menu_reload_seconds = (unsigned int)&target;\n"
        )
        self.rom(struct.pack(">I", 0x80001080) + bytes(4))
        write_object(
            self.original,
            {".rdata": bytes(8)},
            [(NAME, ".rdata", 0, 4, 0x11), ("target", "ABS", 0x80001080, 0)],
            relocations=[(".rdata", 0, 2, "target")],
        )
        layout = self.place()
        self.capture()
        self.linked(layout, content=bytes(8))
        self.assertIsNone(
            data.record_linked(self.project, self.host, self.source, "us", self.original, self.final, layout)
        )
        self.linked(layout, content=struct.pack(">I", 0x80001080) + bytes(4))
        self.assertIsNotNone(
            data.record_linked(self.project, self.host, self.source, "us", self.original, self.final, layout)
        )
        self.assertEqual(self.coverage().manifest["verified_bytes"], 4)

    def test_consumer_requires_current_active_initialized_definition_proof(self):
        layout = self.place()
        self.capture()
        self.linked(layout)
        data.record_linked(self.project, self.host, self.source, "us", self.original, self.final, layout)
        event = attempts.ledger(self.project).latest("native.data", "us")
        payload = event["result"]["value"]["native_data"]
        payload["evidence"][0]["extents"][0]["definition_proof_id"] = "0" * 64
        event["result"]["proof_ids"] = [data.digest(payload)]
        sources = {self.source.stem: (hashlib.sha256(self.source.read_bytes()).hexdigest(), set(), False)}
        report = data.coverage(self.project, "us", sources, event)
        self.assertEqual(report.manifest["verified_bytes"], 0)
        self.assertIn("data.definition.unavailable", [row["key"] for row in report.manifest["causes"]])
