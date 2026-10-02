"""ROM-only ownership and retained coverage do not require existing C or ELF."""

import struct
import unittest

from unbake.layout.planner import carve, complete_providers, correspondence
from unbake.layout.rodata_owners import Span
from unbake.layout.split import Function
from unbake.layout.split_analysis import copy_evidence


def function(name: str, start: int, end: int) -> Function:
    return Function("us", name, start, end, 0x80001000 + start, name, "asm", ())


class PlannerTests(unittest.TestCase):
    def test_private_shared_writable_and_unknown_bytes_cover_the_image(self) -> None:
        image = bytearray(0x90)
        struct.pack_into(">6I", image, 0, 0x3C018000, 0xC4203000, 0xC4223004, 0xE4243008, 0x03E00008, 0)
        struct.pack_into(">4I", image, 0x20, 0x3C018000, 0xC4203004, 0x03E00008, 0)
        struct.pack_into(">4I", image, 0x80, 0x3F800000, 0x40000000, 0x40400000, 0xDEADBEEF)
        ff = [function("alpha", 0, 24), function("beta", 0x20, 0x30)]
        constants = carve(bytes(image), ff, [Span(0x80003000, 0x80, 0x90, 0, "copied")])
        self.assertEqual([p["kind"] for p in constants], ["private", "shared", "writable", "unresolved"])
        self.assertEqual(constants[0]["owners"], ["alpha"])
        self.assertEqual(constants[1]["owners"], ["alpha", "beta"])
        self.assertTrue(constants[1]["name"].startswith("rodata/shared/"))
        providers = complete_providers(bytes(image), ff, constants, tuple(ff))
        self.assertEqual(sum(p["end"] - p["start"] for p in providers), len(image))
        self.assertEqual([p["start"] for p in providers[1:]], [p["end"] for p in providers[:-1]])
        self.assertEqual(providers[0]["start"], 0)
        self.assertEqual(providers[-1]["end"], len(image))

    def test_cross_version_canonical_names_do_not_collide_with_address_labels(self) -> None:
        image = struct.pack(">4I", 0x24020001, 0x03E00008, 0, 0)
        source = Function("us", "func_80001000", 0, 16, 0x80001000, "entry", "asm", ())
        target = Function("eu", "func_80002000", 0, 16, 0x80002000, "entry", "asm", ())
        names = correspondence({"us": image, "eu": image}, {"us": [source], "eu": [target]}, "us")
        self.assertEqual(names, {"us": {0: "func_80001000_us"}, "eu": {0: "func_80001000_us"}})

    def test_copied_loop_retains_destination_and_complete_extent(self) -> None:
        data = bytearray(0x4000)
        code = [
            0x27BDFFF0,
            0x3C04B000,
            0x24842000,
            0x3C058000,
            0x24A53000,
            0x24060003,
            0x8C820000,
            0xACA20000,
            0x24840004,
            0x24A50004,
            0x14C0FFFB,
            0x24C6FFFF,
        ]
        struct.pack_into(">" + "I" * len(code), data, 0x1000, *code)
        self.assertIn((0x2000, 0x2010, 0x80003000), copy_evidence(bytes(data)))

    def test_item_missing_from_naming_version_joins_other_versions(self) -> None:
        body = struct.pack(">4I", 0x24020002, 0x03E00008, 0, 0)
        first = Function("eu", "func_80002000", 0, 16, 0x80002000, "entry", "asm", ())
        second = Function("de", "func_80003000", 0, 16, 0x80003000, "entry", "asm", ())
        inventories = {"us": [], "eu": [first], "de": [second]}
        images = {"us": b"", "eu": body, "de": body}
        names = correspondence(images, inventories, "us")
        self.assertEqual(names, {"us": {}, "eu": {0: "func_80002000_eu"}, "de": {0: "func_80002000_eu"}})
        self.assertEqual(names, correspondence(images, inventories, "de"))

    def test_declared_order_names_an_item_and_ambiguous_bodies_stay_separate(self) -> None:
        body = struct.pack(">4I", 0x24020002, 0x03E00008, 0, 0)
        first = Function("eu", "first", 0, 16, 0x80002000, "entry", "asm", ())
        second = Function("us", "second", 0, 16, 0x80003000, "entry", "asm", ())
        duplicate = Function("us", "duplicate", 16, 32, 0x80003010, "entry", "asm", ())
        names = correspondence({"eu": body, "us": body * 2}, {"eu": [first], "us": [second, duplicate]}, "us")
        self.assertEqual(names, {"eu": {0: "first"}, "us": {0: "second_us", 16: "duplicate_us"}})

    def identity(self, sequences):
        images, inventories = {}, {}
        for v, values in sequences.items():
            code = [word for value in values for word in (0x24020000 | value, 0x03E00008, 0, 0)]
            images[v] = struct.pack(f">{len(code)}I", *code)
            inventories[v] = [function(f"f{i}", i * 16, (i + 1) * 16) for i in range(len(values))]
        evidence = {}
        names = correspondence(images, inventories, next(iter(sequences)), evidence=evidence)
        return names, evidence

    def test_repeated_sequence_joins_by_rom_order_between_shared_anchors(self):
        names, evidence = self.identity({"us": [1, 0, 0, 2], "eu": [1, 0, 0, 2], "de": [1, 0, 0, 2]})
        self.assertEqual(names["us"], names["eu"])
        self.assertEqual(names["us"], names["de"])
        self.assertNotEqual(names["us"][16], names["us"][32])
        self.assertEqual(evidence["de"][16], "anchor-sequence")

    def test_insertions_changes_and_reordered_anchors_refuse_positional_join(self):
        for other, reason in (
            ([1, 0, 2], "sequence-mismatch"),
            ([1, 0, 3, 2], "sequence-mismatch"),
            ([2, 0, 0, 1], "anchor-order"),
        ):
            with self.subTest(other=other):
                names, evidence = self.identity({"us": [1, 0, 0, 2], "eu": other})
                self.assertNotIn(names["us"][16], names["eu"].values())
                self.assertIn(reason, evidence["us"][16])

    def test_duplicate_alignment_can_join_a_subset_without_the_naming_version(self):
        names, evidence = self.identity({"us": [], "eu": [1, 0, 0, 2], "de": [1, 0, 0, 2]})
        self.assertEqual(names["eu"], names["de"])
        self.assertEqual(evidence["de"][32], "anchor-sequence")

    def test_pairwise_alignment_conflicts_reject_all_proposals(self):
        # A's unique 0 already corresponds to B's unique 0. C has two 0s:
        # the A/C window suggests the first, while B/C suggests the second.
        names, evidence = self.identity({"a": [1, 0, 2, 3, 4], "b": [1, 2, 3, 0, 4], "c": [1, 0, 2, 3, 0, 4]})
        self.assertEqual(names["a"][16], names["b"][48])
        for at in (16, 64):
            self.assertNotIn(names["c"][at], names["a"].values())
            self.assertEqual(evidence["c"][at], "repeated-body-alignment-conflict")

    def test_inventory_order_is_not_used_as_rom_order(self):
        body = struct.pack(
            ">16I", 0x24020001, 0x03E00008, 0, 0, 0x03E00008, 0, 0, 0, 0x03E00008, 0, 0, 0, 0x24020002, 0x03E00008, 0, 0
        )
        ff = [function(f"f{i}", i * 16, (i + 1) * 16) for i in range(4)]
        names = correspondence({"us": body, "eu": body}, {"us": ff, "eu": list(reversed(ff))}, "us")
        self.assertEqual(names["us"], names["eu"])

    def test_matching_normalized_zeros_require_the_same_relocation_roles(self):
        images = {
            "us": struct.pack(">4I", 0x3C018001, 0x8C220000, 0x03E00008, 0),
            "eu": struct.pack(">4I", 0x3C010000, 0x8C220000, 0x03E00008, 0),
        }
        ff = {v: [function("entry", 0, 16)] for v in images}
        names = correspondence(images, ff, "us")
        self.assertNotEqual(names["us"][0], names["eu"][0])
