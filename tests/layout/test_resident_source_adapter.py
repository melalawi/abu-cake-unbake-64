"""Real imported storage copies yield to ROM ownership without relaxing admission."""

import gzip
import hashlib
import json
import re
import struct
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from tests.kit import TempCase
from unbake import process, runner
from unbake.config import Held, Version
from unbake.decomp import checks, rom
from unbake.layout import resident, split
from unbake.objects.elf import Object
from unbake.objects.rodata import unresolved_sections

FIXTURE = Path(__file__).parent / "fixtures/ragewars_resident_storage"
PROOFS = json.loads(gzip.decompress((FIXTURE / "proof.json.gz").read_bytes()))
SOURCES = sorted({row["function"] for row in PROOFS})


class ResidentSourceAdapterTests(TempCase):
    def source(self, function, label):
        return (FIXTURE / "source" / function / f"{label}.c").read_text()

    def test_exact_real_source_native_and_mapping_payloads_are_pinned(self):
        provenance = json.loads((FIXTURE / "provenance.json").read_text())
        self.assertEqual(provenance["native_cases"], 29)
        for relative, record in provenance["files"].items():
            data = (FIXTURE / relative).read_bytes()
            self.assertEqual(len(data), record["bytes"], relative)
            self.assertEqual(hashlib.sha256(data).hexdigest(), record["sha256"], relative)

    def test_one_bulk_adapter_preserves_every_body_and_real_holding_distinction(self):
        self.assertEqual(len(SOURCES), 6)
        self.assertEqual(len(PROOFS), 29)
        versions = {function: {r["version"] for r in PROOFS if r["function"] == function} for function in SOURCES}
        self.assertEqual(versions["func_80212544_eu"], {"eu", "eu-x", "us", "us-rev1"})
        for function in SOURCES:
            with self.subTest(function=function):
                before, after = self.source(function, "before"), self.source(function, "after")
                self.assertTrue(any(row.rule == "resident-storage" for row in checks.run(before)))
                self.assertEqual(resident.deleted(before, function), after)
                self.assertFalse(any(row.rule == "resident-storage" for row in checks.run(after)))
                self.assertEqual(resident.deleted(after, function), after)
                self.assertEqual(after.rstrip(), before.partition(resident.MARKER)[0].rstrip())
                self.assertEqual(checks.fakematches(after), ())
                for row in (r for r in PROOFS if r["function"] == function):
                    self.assertEqual(hashlib.sha256(before.encode()).hexdigest(), row["source_sha256"])
                    self.assertEqual(hashlib.sha256(after.encode()).hexdigest(), row["after_sha256"])
                    for label in ("before", "after"):
                        self.assertEqual(row["native"][label]["words"], row["rom_text"])
                        self.assertEqual(row["native"][label]["allocated_linked_sections"], [".text"])
                    self.assertEqual(
                        row["native"]["before"]["text_relocations"], row["native"]["after"]["text_relocations"]
                    )
                    self.assertEqual(
                        row["native"]["before"]["placed_relocations"], row["native"]["after"]["placed_relocations"]
                    )
                    self.assertEqual(
                        row["native"]["before"]["linked_symbols"], row["native"]["after"]["linked_symbols"]
                    )
                    self.assertEqual(row["native"]["after"]["manufactured_definitions"], 0)

    def test_real_storage_names_still_used_or_malformed_are_refused(self):
        for function in SOURCES:
            before = self.source(function, "before")
            name = re.search(r"\bunbake_rodata_\w+", before)[0]
            used = before + f"\nconst void *resident_address(void) {{ return &{name}; }}\n"
            with self.subTest(function=function), self.assertRaisesRegex(Held, f"source still uses {name}"):
                resident.deleted(used, function)
            # Some bodies have their own conditionals: corrupt the storage
            # block's final directive, rather than a source-body directive.
            head, marker, block = before.partition(resident.MARKER)
            malformed = head + marker + block.replace("#endif", "int unexpected;\n#endif", 1)
            with self.subTest(function=function), self.assertRaisesRegex(Held, "expected #endif"):
                resident.deleted(malformed, function)

    def test_bounded_rom_reads_keep_original_resident_mapping_and_byte_identity(self):
        for number, row in enumerate(PROOFS):
            with self.subTest(function=row["function"], version=row["version"]):
                image = self.root / f"resident-{number}.z64"
                data = row["resident_data"]
                with image.open("wb") as stream:
                    stream.seek(row["text_rom_offset"])
                    stream.write(bytes.fromhex(row["rom_text"]))
                    for cell in data:
                        stream.seek(cell["rom_offset"])
                        stream.write(bytes.fromhex(cell["bytes"]))
                yaml = self.root / f"resident-{number}.yaml"
                yaml.write_text(
                    "segments:\n  - name: native\n    type: code\n"
                    f"    start: 0x{row['text_rom_offset']:X}\n    vram: 0x{row['text_address']:X}\n"
                    "    subsegments:\n"
                    f"      - [0x{row['text_rom_offset']:X}, c, {row['function']}]\n"
                    f"  - [0x{row['text_rom_offset'] + row['text_size']:X}]\n"
                )
                version = Version(row["version"], image, "", yaml, self.root / "symbols.txt", ())
                copies = Mock(return_value=row["resident_mappings"])
                with (
                    patch.object(split, "layout", wraps=split.layout) as maps,
                    patch.object(rom, "target", wraps=rom.target) as read,
                ):
                    reader = rom.rom_reader(version, copies)
                    self.assertEqual(reader(row["text_address"], row["text_size"]), bytes.fromhex(row["rom_text"]))
                    for cell in data:
                        self.assertEqual(reader(cell["address"], cell["size"]), bytes.fromhex(cell["bytes"]))
                        self.assertEqual(
                            reader.span(cell["address"], cell["size"]).offset
                            + cell["address"]
                            - reader.span(cell["address"], cell["size"]).address,
                            cell["rom_offset"],
                        )
                    self.assertEqual(maps.call_count, 1)
                    self.assertEqual(copies.call_count, int(bool(data)))
                    self.assertEqual(read.call_count, row["ROM_reads"])
                    spans = [call.args[1] for call in read.call_args_list]
                    self.assertEqual(sum(span.size for span in spans), row["ROM_read_bytes"])
                    self.assertEqual(
                        [(s.address, s.offset, s.size) for s in spans],
                        [
                            (row["text_address"], row["text_rom_offset"], row["text_size"]),
                            *((c["address"], c["rom_offset"], c["size"]) for c in data),
                        ],
                    )
                    before = read.call_count
                    with self.assertRaisesRegex(Held, "unmapped or ambiguous"):
                        reader(0x7FFFFFFC, 4)
                    self.assertEqual(read.call_count, before)

    def test_exact_native_text_does_not_own_unreferenced_manufactured_data(self):
        for version in ("de", "eu", "eu-x", "us", "us-rev1"):
            with self.subTest(version=version):
                row = next(r for r in PROOFS if r["function"] == "func_80231474_de" and r["version"] == version)
                objects = {}
                for label in ("before", "after"):
                    obj = Object(FIXTURE / "native" / version / f"{label}-object.elf32")
                    objects[label] = obj
                    manufactured = [
                        s for symbols in obj.symbols.values() for s in symbols if s["name"].startswith("unbake_rodata_")
                    ]
                    self.assertEqual(len(manufactured), row["native"][label]["manufactured_definitions"])
                    self.assertEqual(unresolved_sections(obj), [])
                    placed = Object(FIXTURE / "native" / version / f"{label}-placed.elf32")
                    self.assertEqual(unresolved_sections(placed), [])
                    linked = Object(FIXTURE / "native" / version / f"{label}-linked.elf32")
                    self.assertEqual(linked.content(linked.section(".text")), bytes.fromhex(row["rom_text"]))
                    allocated = [
                        linked.names[i] for i, section in enumerate(linked.sections) if section[2] & 2 and section[5]
                    ]
                    self.assertEqual(allocated, [".text"])
                self.assertIsNotNone(objects["before"].section(".rdata"))
                self.assertIsNone(objects["after"].section(".rdata"))

    def test_redirected_native_reference_cannot_link_unproved_storage_despite_unchanged_text(self):
        original = Object(FIXTURE / "native/de/before-object.elf32")
        text = original.section(".text")
        target = next(
            symbol
            for symbols in original.symbols.values()
            for symbol in symbols
            if symbol["name"] == "unbake_rodata_800C2F68_4"
        )
        modified = bytearray(original.data)
        changed = 0
        for section in original.sections:
            if section[1] != 9 or section[7] != text:
                continue
            for offset in range(section[4], section[4] + section[5], 8):
                _where, info = struct.unpack_from(">II", modified, offset)
                symbol = original.symbols[section[6]][info >> 8]
                if symbol["name"] == "D_800C2F68_de":
                    self.assertEqual(target["table"], section[6])
                    struct.pack_into(">I", modified, offset + 4, target["index"] << 8 | info & 255)
                    changed += 1
        self.assertEqual(changed, 2)
        path = self.root / "unproved.o"
        path.write_bytes(modified)
        proposed = Object(path)
        self.assertEqual(proposed.content(text), original.content(text))
        self.assertEqual(unresolved_sections(proposed), [".rdata"])
        script = self.root / "versions/de/fixture.ld"
        script.parent.mkdir(parents=True)
        script.write_text("OUTPUT_ARCH(mips)\nSECTIONS { .text : { *(.text) } /DISCARD/ : { *(*) } }\n")
        project = SimpleNamespace(root=self.root, name="fixture")
        with (
            patch.object(process, "run_tool") as tools,
            self.assertRaisesRegex(Held, "link.unproved: .*VERSION de: unplaced .rdata"),
        ):
            runner.link(project, SimpleNamespace(), path, "de", SimpleNamespace(), self.root, self.root / "source.c")
        tools.assert_not_called()

    def test_other_functions_resident_rows_and_version_aliases_do_not_change_owner(self):
        other = [
            c
            for r in PROOFS
            if r["function"] == "func_80226950_de"
            for c in r["resident_data"]
            if "func_8028798C_de" in c["backing"]["path"]
        ]
        self.assertTrue(other)
        self.assertTrue(all(cell["backing"]["kind"] == "rodata" for cell in other))
        rows = [r for r in PROOFS if r["function"] == "func_80231474_de"]
        addresses = {r["version"]: r["native"]["after"]["linked_symbols"]["D_800C2F68_de"] for r in rows}
        self.assertEqual(len(set(addresses.values())), 5)
        for row in rows:
            self.assertIn(addresses[row["version"]], {cell["address"] for cell in row["resident_data"]})
