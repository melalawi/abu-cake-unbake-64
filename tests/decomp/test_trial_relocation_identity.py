"""Byte-proved pool placement and address identity at the trial diff boundary."""

import copy
import shutil
import struct
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from tests.decomp.support import assemble
from tests.elf_fixture import write_object
from tests.support import test_policy
from unbake.decomp.relocations import jump_table_differences
from unbake.decomp.score import diff
from unbake.decomp.trial import comparison_identical
from unbake.decomp.trial_compare import compare_object


class TrialRelocationIdentityTests(unittest.TestCase):
    def setUp(self):
        from tests.objdiff_fixture import install

        install(self)

    def test_native_literal_with_shared_storage_outside_private_slice(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            shutil.copyfile(Path(__file__).parents[1] / "fixture/config.toml", root / "config.toml")
            version = root / "versions/us"
            version.mkdir(parents=True)
            (version / "fixture.yaml").write_text(
                "name: fixture\nsegments:\n"
                "  - name: code\n    type: code\n    start: 0x40\n    vram: 0x80002000\n"
                "    subsegments:\n      - [0x40, asm, alpha]\n"
                "  - name: constants\n    type: code\n    start: 0x100\n    vram: 0x80001000\n"
                "    subsegments:\n      - [0x100, data, pool]\n  - [0x118]\n"
            )
            (version / "symbol_addrs.txt").write_text("alpha = 0x80002000;\nconstant = 0x80001010;\n")
            image = bytearray(0x118)
            image[0x40:0x50] = struct.pack(">4I", 0x3C018000, 0xC4201010, 0x03E00008, 0)
            image[0x104:0x108] = bytes.fromhex("3f800000")
            image[0x110:0x114] = bytes.fromhex("3f000000")
            (root / "roms").mkdir(exist_ok=True)
            (root / "roms/baserom.us.z64").write_bytes(image)
            generation = root / "build/us.0"
            generation.mkdir(parents=True)
            body = (
                ".set noreorder\n.text\n.globl alpha\n.type alpha,@function\nalpha:\n"
                "lui $at,%hi({name})\nlwc1 $f0,%lo({name})($at)\njr $ra\nnop\n.size alpha,.-alpha\n"
            )
            target = assemble(root, "target", body.format(name="constant"))
            slice_ = {"owner": "alpha", "path": "rodata/private", "start": 0x110, "end": 0x114, "address": 0x80001010}
            for literal, shared, exact in (
                (0x3F000000, 0x3F800000, True),
                (0x40000000, 0x3F800000, False),
                (0x3F000000, 0x40000000, False),
            ):
                with self.subTest(literal=literal, shared=shared):
                    candidate = assemble(
                        root,
                        "candidate",
                        body.format(name="literal")
                        + f".section .rdata\nliteral: .word 0x{literal:X}\n"
                        + f".globl unbake_rodata_80001004_4\nunbake_rodata_80001004_4: .word 0x{shared:X}\n",
                    )
                    before = candidate.read_bytes()
                    with patch("unbake.project_tools.extract.pool_rows", return_value=[slice_]):
                        document = diff(
                            test_policy(root),
                            "us",
                            "alpha",
                            target,
                            candidate,
                            root / "diff.json",
                            generation=generation,
                        )
                    self.assertEqual(candidate.read_bytes(), before)
                    result = compare_object("us", document, "alpha")
                    self.assertEqual(comparison_identical(result), exact, result.lines)
                    if exact:
                        self.assertEqual(document["pool_placement"]["sections"]["[.rdata]"], 0x80001004)
                    else:
                        self.assertGreater(result.typed["relocation"], 0)

    def test_transferred_tables_compare_resident_pointer_representation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "config.toml").touch()
            generation = root / "build/us.0"
            generation.mkdir(parents=True)
            reader = Mock(return_value=bytes.fromhex("00002018"))
            reader.find_span.return_value = True
            reader.table_entry.return_value = 0x80002018
            for section, addend, expected in (
                (".unbake_pool_80001000", 0x80000018, []),
                (".unbake_pool_80001000", 0x8000001C, ["draft 0x0000201C"]),
                (".rdata", 0x18, []),
                (".rdata", 0x80000018, ["draft 0x00002018"]),
            ):
                with self.subTest(section=section, addend=addend):
                    candidate = write_object(
                        root / "table.o",
                        {".text": bytes(32), section: struct.pack(">I", addend)},
                        [("text", ".text", 0, 0, 3)],
                        relocations=[(section, 0, 2, "text")],
                    )
                    with (
                        patch("unbake.config.load"),
                        patch("unbake.decomp.rom.project_reader", return_value=reader),
                    ):
                        result = jump_table_differences(
                            generation,
                            "us",
                            candidate,
                            {"[.text]": 0x80002000, f"[{section}]": 0x80001000},
                            {},
                            section_name=section,
                        )
                    if expected:
                        self.assertEqual(len(result), 1)
                        self.assertIn(expected[0], result[0])
                    else:
                        self.assertEqual(result, [])

    def test_alias_and_version_suffix_require_same_effective_address(self):
        for target_name, draft_name, kind in (
            ("D_80001000_de", "D_80001000_eu", 5),
            ("callee_de", "callee_alias", 4),
            ("jtbl_80001000", "[.rdata]", 6),
        ):
            instruction = {
                "address": 0,
                "parts": [{"opcode": "lui"}, {"arg": {"opaque": "at"}}, {"arg": {"reloc": True}}],
                "relocation": {"type": kind, "target_symbol": 1},
            }
            document = {
                side: {
                    "symbols": [
                        {
                            "name": "alpha",
                            "kind": "SYMBOL_FUNCTION",
                            "size": 4,
                            "match_percent": 99,
                            "instructions": [
                                {"diff_kind": "DIFF_ARG_MISMATCH", "instruction": copy.deepcopy(instruction)}
                            ],
                        },
                        {"name": name},
                    ]
                }
                for side, name in (("left", target_name), ("right", draft_name))
            }
            document["symbol_addresses"] = {target_name: 0x80001000, draft_name: 0x80001000}
            with self.subTest(target=target_name, draft=draft_name):
                self.assertTrue(comparison_identical(compare_object("us", document, "alpha")))
                document["symbol_addresses"][draft_name] += 4
                self.assertEqual(compare_object("us", document, "alpha").typed["relocation"], 1)
                document["symbol_addresses"][draft_name] -= 4
                document["right"]["symbols"][0]["instructions"][0]["instruction"]["parts"][1]["arg"]["opaque"] = "v0"
                self.assertEqual(compare_object("us", document, "alpha").typed["register"], 1)

    def test_encoded_calls_use_pc_region_and_keep_wrong_callees(self):
        from unbake.decomp.relocations import paired_relocation_addresses

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = write_object(
                root / "target.o", {".text": struct.pack(">I", 0x0C000400)}, [("alpha", ".text", 0, 4)]
            )
            candidate = write_object(
                root / "candidate.o",
                {".text": struct.pack(">I", 0x0C000000)},
                [("alpha", ".text", 0, 4), ("callee", None, 0, 0)],
                relocations=[(".text", 0, 4, "callee")],
            )
            left = {"address": 0, "parts": [{"opcode": {"mnemonic": "jal"}}, {"arg": {"opaque": "func_00001000"}}]}
            right = {
                "address": 0,
                "parts": [{"opcode": {"mnemonic": "jal"}}, {"arg": {"reloc": True}}],
                "relocation": {"type": 4, "target_symbol": 1},
            }
            for address, exact in ((0x80001000, True), (0x80001004, False), (0x90001000, False), (None, False)):
                with self.subTest(address=address):
                    sections = {"[.text]": 0x80002000}
                    addresses = {} if address is None else {"callee": address}
                    document = {
                        "section_addresses": {"left": sections, "right": sections},
                        "relocation_addresses": {
                            "left": paired_relocation_addresses(target, sections, addresses),
                            "right": paired_relocation_addresses(candidate, sections, addresses),
                        },
                    }
                    for side, ins in (("left", left), ("right", right)):
                        document[side] = {
                            "symbols": [
                                {
                                    "name": "alpha",
                                    "kind": "SYMBOL_FUNCTION",
                                    "size": 4,
                                    "match_percent": 99,
                                    "instructions": [{"diff_kind": "DIFF_ARG_MISMATCH", "instruction": ins}],
                                },
                                {"name": "callee"},
                            ]
                        }
                    result = compare_object("us", document, "alpha")
                    self.assertEqual(comparison_identical(result), exact, result.lines)

    def test_encoded_local_jump_and_relocated_label_share_function_offset(self):
        from unbake.decomp.relocations import paired_relocation_addresses

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = write_object(
                root / "target.o", {".text": struct.pack(">3I", 0x08000802, 0, 0)}, [("alpha", ".text", 0, 12)]
            )
            candidate = write_object(
                root / "candidate.o",
                {".text": struct.pack(">3I", 0x08000002, 0, 0)},
                [("alpha", ".text", 0, 12), ("text", ".text", 0, 0, 3)],
                relocations=[(".text", 0, 4, "text")],
            )
            sections = {"[.text]": 0x80002000}
            document = {
                "section_addresses": {"left": sections, "right": sections},
                "relocation_addresses": {
                    "left": paired_relocation_addresses(target, sections, {}),
                    "right": paired_relocation_addresses(candidate, sections, {}),
                },
            }
            for side in ("left", "right"):
                ins = {"address": 0, "parts": [{"opcode": {"mnemonic": "j"}}, {"arg": {"opaque": "local"}}]}
                if side == "right":
                    ins.update(branch_dest=8, relocation={"type": 4, "target_symbol": 1, "addend": 8})
                document[side] = {
                    "symbols": [
                        {
                            "name": "alpha",
                            "kind": "SYMBOL_FUNCTION",
                            "size": 12,
                            "match_percent": 99,
                            "instructions": [{"diff_kind": "DIFF_ARG_MISMATCH", "instruction": ins}],
                        },
                        {"name": "[.text]"},
                    ]
                }
            self.assertTrue(comparison_identical(compare_object("us", document, "alpha")))

    def test_local_literal_cannot_borrow_a_global_symbol_address_by_name(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            generation = root / "build/us.0"
            generation.mkdir(parents=True)
            (generation / "game.map").write_text(" 0x80002000 alpha\n 0x80001000 constant\n")
            body = (
                ".set noreorder\n.text\n.globl alpha\n.type alpha,@function\nalpha:\n"
                "lui $at,%hi(constant)\nlwc1 $f0,%lo(constant)($at)\n"
                "jr $ra\nnop\n.size alpha,.-alpha\n"
            )
            target = assemble(root, "target", body)
            for content in (0x3F000000, 0x40000000):
                with self.subTest(content=content):
                    candidate = assemble(
                        root, "candidate", body + f".section .rdata\n.globl constant\nconstant: .word 0x{content:X}\n"
                    )
                    document = diff(
                        test_policy(root), "us", "alpha", target, candidate, root / "diff.json", generation=generation
                    )
                    self.assertEqual(document["unplaced_relocation_offsets"]["right"], [0, 4])
                    result = compare_object("us", document, "alpha")
                    self.assertFalse(comparison_identical(result))
                    self.assertEqual(result.typed["relocation"], 2)

    def test_raw_address_source_operand_is_not_a_named_relocation(self):
        left = {
            "address": 0,
            "parts": [{"opcode": {"mnemonic": "lui"}}, {"arg": {"opaque": "v0"}}, {"arg": {"reloc": True}}],
            "relocation": {"type": 5, "target_symbol": 1},
        }
        right = {
            "address": 0,
            "parts": [{"opcode": {"mnemonic": "lui"}}, {"arg": {"opaque": "v0"}}, {"arg": {"unsigned": 0xA000}}],
        }
        document = {"symbol_addresses": {"D_A0000000": 0xA0000000}}
        for side, ins in (("left", left), ("right", right)):
            document[side] = {
                "symbols": [
                    {
                        "name": "alpha",
                        "kind": "SYMBOL_FUNCTION",
                        "size": 4,
                        "match_percent": 99,
                        "instructions": [{"diff_kind": "DIFF_ARG_MISMATCH", "instruction": ins}],
                    },
                    {"name": "D_A0000000"},
                ]
            }
        self.assertEqual(compare_object("us", document, "alpha").typed["relocation"], 1)

    def test_call_between_hi_lo_uses_does_not_consume_the_pair(self):
        from unbake.decomp.relocations import paired_relocation_addresses

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            candidate = write_object(
                root / "candidate.o",
                {".text": struct.pack(">3I", 0x3C020000, 0x0C000000, 0x24420004)},
                [("callee", None, 0, 0)],
                relocations=[(".text", 0, 5, "callee"), (".text", 4, 4, "callee"), (".text", 8, 6, "callee")],
            )
            self.assertEqual(
                paired_relocation_addresses(candidate, {"[.text]": 0x80002000}, {"callee": 0x80001000}),
                {0: (5, 0x80001004), 4: (4, 0x80001000), 8: (6, 0x80001004)},
            )
