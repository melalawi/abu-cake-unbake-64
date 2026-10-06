"""ROM-only ownership and retained coverage do not require existing C or ELF."""

import json
import struct
import unittest

from tests.work.test_shape import SHAPES
from unbake.config import SymbolPolicy
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
        providers = complete_providers(bytes(image), ff, constants, tuple(ff), SHAPES)
        self.assertEqual(sum(p["end"] - p["start"] for p in providers), len(image))
        self.assertEqual([p["start"] for p in providers[1:]], [p["end"] for p in providers[:-1]])
        self.assertEqual(providers[0]["start"], 0)
        self.assertEqual(providers[-1]["end"], len(image))

    def test_cross_version_canonical_names_do_not_collide_with_address_labels(self) -> None:
        image = struct.pack(">4I", 0x24020001, 0x03E00008, 0, 0)
        source = Function("us", "func_80001000", 0, 16, 0x80001000, "entry", "asm", ())
        target = Function("eu", "func_80002000", 0, 16, 0x80002000, "entry", "asm", ())
        names = correspondence(
            {"us": image, "eu": image}, {"us": [source], "eu": [target]}, "us", symbol_policy=SymbolPolicy(0.9, 0.1)
        )
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
        names = correspondence(images, inventories, "us", symbol_policy=SymbolPolicy(0.9, 0.1))
        self.assertEqual(names, {"us": {}, "eu": {0: "func_80002000_eu"}, "de": {0: "func_80002000_eu"}})
        self.assertEqual(names, correspondence(images, inventories, "de", symbol_policy=SymbolPolicy(0.9, 0.1)))

    def test_declared_order_names_an_item_and_ambiguous_bodies_stay_separate(self) -> None:
        body = struct.pack(">4I", 0x24020002, 0x03E00008, 0, 0)
        first = Function("eu", "first", 0, 16, 0x80002000, "entry", "asm", ())
        second = Function("us", "second", 0, 16, 0x80003000, "entry", "asm", ())
        duplicate = Function("us", "duplicate", 16, 32, 0x80003010, "entry", "asm", ())
        names = correspondence(
            {"eu": body, "us": body * 2},
            {"eu": [first], "us": [second, duplicate]},
            "us",
            symbol_policy=SymbolPolicy(0.9, 0.1),
        )
        self.assertEqual(names, {"eu": {0: "first"}, "us": {0: "second_us", 16: "duplicate_us"}})

    def identity(self, sequences):
        images, inventories = {}, {}
        for v, values in sequences.items():
            code = [word for value in values for word in (0x24020000 | value, 0x03E00008, 0, 0)]
            images[v] = struct.pack(f">{len(code)}I", *code)
            inventories[v] = [function(f"f{i}", i * 16, (i + 1) * 16) for i in range(len(values))]
        evidence = {}
        names = correspondence(
            images, inventories, next(iter(sequences)), symbol_policy=SymbolPolicy(0.9, 0.1), evidence=evidence
        )
        return names, evidence

    def test_repeated_sequence_joins_by_rom_order_between_shared_anchors(self):
        names, evidence = self.identity({"us": [1, 0, 0, 2], "eu": [1, 0, 0, 2], "de": [1, 0, 0, 2]})
        self.assertEqual(names["us"], names["eu"])
        self.assertEqual(names["us"], names["de"])
        self.assertNotEqual(names["us"][16], names["us"][32])
        self.assertEqual(evidence["de"][16], "anchor-sequence")

    def test_insertions_changes_and_reordered_anchors_refuse_positional_join(self):
        for other, reason in (
            ([1, 0, 2], "alignment-ambiguous"),
            ([1, 0, 3, 2], "leaf-similarity-ambiguous"),
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
            self.assertEqual(evidence["c"][at], "symbol-position-conflict")

    def test_inventory_order_is_not_used_as_rom_order(self):
        body = struct.pack(
            ">16I", 0x24020001, 0x03E00008, 0, 0, 0x03E00008, 0, 0, 0, 0x03E00008, 0, 0, 0, 0x24020002, 0x03E00008, 0, 0
        )
        ff = [function(f"f{i}", i * 16, (i + 1) * 16) for i in range(4)]
        names = correspondence(
            {"us": body, "eu": body}, {"us": ff, "eu": list(reversed(ff))}, "us", symbol_policy=SymbolPolicy(0.9, 0.1)
        )
        self.assertEqual(names["us"], names["eu"])

    def test_matching_normalized_zeros_require_the_same_relocation_roles(self):
        images = {
            "us": struct.pack(">4I", 0x3C018001, 0x8C220000, 0x03E00008, 0),
            "eu": struct.pack(">4I", 0x3C010000, 0x8C220000, 0x03E00008, 0),
        }
        ff = {v: [function("entry", 0, 16)] for v in images}
        names = correspondence(images, ff, "us", symbol_policy=SymbolPolicy(0.9, 0.1))
        self.assertNotEqual(names["us"][0], names["eu"][0])


class SymbolIdentityTests(unittest.TestCase):
    def identity(self, changed=(0x24020003,), *, extra=None, caller_target=0x80001020, reverse=False):
        images, inventories = {}, {}
        for v, body in (("us", (0x24020003,)), ("eu", changed)):
            image = bytearray(0xC0)
            bodies = {
                0: (0x24020001, 0x03E00008, 0),
                0x20: (*body, 0x03E00008, 0),
                0x60: (0x24020002, 0x03E00008, 0),
                0x80: (0x0C000000 | ((caller_target if v == "eu" else 0x80001020) >> 2 & 0x3FFFFFF), 0, 0x03E00008, 0),
            }
            if extra and v == "eu":
                bodies[0x40] = extra
            if reverse and v == "eu":
                bodies[0], bodies[0x60] = bodies[0x60], bodies[0]
            ff = []
            for at, code in bodies.items():
                struct.pack_into(f">{len(code)}I", image, at, *code)
                ff.append(function(f"func_{0x80001000 + at:08X}", at, at + len(code) * 4))
            images[v], inventories[v] = bytes(image), ff
        evidence, details = {}, {}
        names = correspondence(
            images, inventories, "us", symbol_policy=SymbolPolicy(0.9, 0.1), evidence=evidence, symbol_evidence=details
        )
        return names, evidence, details

    def test_inserted_instruction_keeps_one_symbol_and_independent_bodies(self):
        names, evidence, details = self.identity((0x24020003, 0x24420001))
        self.assertEqual(names["us"][0x20], names["eu"][0x20])
        self.assertEqual(names["us"][0x80], names["eu"][0x80])
        self.assertEqual(evidence["eu"][0x20], "anchor-call-graph")
        self.assertEqual(details["eu"][0x20]["callers"], [["eu", 0x80]])
        self.assertTrue(details["eu"][0x20]["anchor_positions"])

    def test_symbol_evidence_is_reproducible_across_process_hash_seeds(self):
        outputs = [json.dumps(self.identity((0x24020003, 0x24420001)), sort_keys=True) for _ in range(3)]
        self.assertTrue(json.loads(outputs[0]))
        self.assertEqual(len(set(outputs)), 1)

    def test_same_name_address_and_bounds_do_not_override_graph_mismatch(self):
        names, evidence, _ = self.identity((0x24020004,), caller_target=0x80001060)
        self.assertNotEqual(names["us"][0x20], names["eu"][0x20])
        self.assertEqual(evidence["eu"][0x20], "symbol-graph-mismatch")

    def test_inserted_function_refuses_equal_position_assumption(self):
        names, evidence, _ = self.identity((0x24020004,), extra=(0x24020005, 0x03E00008, 0))
        self.assertNotEqual(names["us"][0x20], names["eu"][0x20])
        self.assertEqual(evidence["eu"][0x20], "symbol-alignment-insertion-deletion")

    def test_indirect_callee_and_reversed_anchors_remain_separate(self):
        for options, reason in (
            ({"changed": (0x0320F809, 0, 0x24020004)}, "symbol-graph-indirect"),
            ({"changed": (0x24020004,), "reverse": True}, "symbol-anchor-order"),
        ):
            with self.subTest(options=options):
                names, evidence, _ = self.identity(**options)
                self.assertNotEqual(names["us"][0x20], names["eu"][0x20])
                self.assertEqual(evidence["eu"][0x20], reason)

    def test_local_switch_requires_guard_mapping_and_every_entry_inside_the_body(self):
        from unbake.layout.symbol_identity import graph, local_switches
        from unbake.project.flow import Span

        image = bytearray(0x300)
        code = [
            0x2C620002,
            0x10400007,
            0x00009821,
            0x00031080,
            0x3C018000,
            0x00220821,
            0x8C221200,
            0x00400008,
            0,
            0x24020003,
            0x03E00008,
            0,
        ]
        struct.pack_into(">12I", image, 0, *code)
        struct.pack_into(">2I", image, 0x200, 0x1024, 0x1028)
        f = function("switch", 0, 48)
        spans = [Span(start=0, end=len(image), address=0x80001000)]
        switches = local_switches(bytes(image), f, spans)
        self.assertEqual(switches[7]["count"], 2)
        self.assertEqual(switches[7]["entry_bias"], 0x80000000)
        self.assertEqual(graph({"us": bytes(image)}, {"us": [f]}, loaded_spans={"us": spans})[2], {})
        self.assertEqual(local_switches(bytes(image), f, []), {})
        # One external entry invalidates the complete local-switch proof.
        struct.pack_into(">I", image, 0x204, 0x1040)
        self.assertEqual(local_switches(bytes(image), f, spans), {})
        self.assertEqual(
            graph({"us": bytes(image)}, {"us": [f]}, loaded_spans={"us": spans})[2],
            {("us", 0): "symbol-graph-indirect"},
        )
        struct.pack_into(">I", image, 0x204, 0x1028)
        struct.pack_into(">I", image, 4, 0x14400007)  # bne admits the unbounded case
        self.assertEqual(local_switches(bytes(image), f, spans), {})

    def test_tail_branch_resolves_unique_peer_body_and_ambiguous_entries_refuse(self):
        from unbake.layout.symbol_identity import graph

        image = struct.pack(">8I", 0x10000004, 0, 0x03E00008, 0, 0x24020003, 0x03E00008, 0, 0)
        ff = [function("caller", 0, 16), function("callee", 16, 32)]
        outgoing, incoming, unresolved = graph({"us": image}, {"us": ff})
        self.assertEqual(outgoing["us", 0], {("us", 16)})
        self.assertEqual(incoming["us", 16], {("us", 0)})
        self.assertFalse(unresolved)
        # Two executable placements at the target prevent address resolution.
        ff.append(Function("us", "overlay", 32, 48, ff[1].address, "overlay", "asm", ()))
        jump = struct.pack(">I", 0x08000000 | (ff[1].address >> 2 & 0x03FFFFFF)) + image[4:]
        self.assertEqual(graph({"us": jump}, {"us": ff})[2]["us", 0], "symbol-graph-target-unresolved")
