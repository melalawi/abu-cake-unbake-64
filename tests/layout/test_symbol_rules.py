"""Independent checks for alignment ambiguity and simultaneous version joins."""

import json
import struct
import unittest
from itertools import combinations, pairwise

from unbake.layout.planner import correspondence
from unbake.layout.split import Function
from unbake.layout.symbol_identity import alignment
from unbake.config import SymbolPolicy


class SymbolRulesTests(unittest.TestCase):
    def test_alignment_forced_pairs_agree_with_exhaustive_monotone_matchings(self):
        for scores in (
            [[0.95, 0], [0, 0.95]],
            [[0, 0.95], [0.95, 0]],
            [[0.95, 0.95], [0.95, 0.95]],
            [[0.95, 0, 0], [0, 0.91, 0.95]],
        ):
            with self.subTest(scores=scores):
                eligible = {(i, j) for i, row in enumerate(scores) for j, score in enumerate(row) if score > 0}
                candidates = sorted(eligible)
                matchings = []
                for count in range(len(candidates) + 1):
                    for selected in combinations(candidates, count):
                        if all(a[0] < b[0] and a[1] < b[1] for a, b in pairwise(selected)):
                            matchings.append((sum(round(scores[i][j] * 1_000_000) for i, j in selected), set(selected)))
                best = max(score for score, _ in matchings)
                expected = set.intersection(*(pairs for score, pairs in matchings if score == best))
                self.assertEqual(alignment(scores, eligible), expected)

    def fixture(self):
        images, inventories = {}, {}
        for version, changes in (("de", ()), ("eu", (9,)), ("us", (9, 13))):
            leaf = [0x24820064, *[0x30420000 | (100 + i) for i in range(1, 18)], 0x03E00008, 0]
            for index in changes:
                leaf[index] ^= 1
            bodies = ([0x3C028001, 0x03E00008, 0], leaf, [0x3C038002, 0x03E00008, 0])
            code, ff = [], []
            for index, body in enumerate(bodies):
                start = len(code) * 4
                code.extend(body)
                ff.append(
                    Function(
                        version,
                        f"entry_{version}_{index}",
                        start,
                        len(code) * 4,
                        0x80001000 + start,
                        f"entry_{version}_{index}",
                        "asm",
                        (),
                    )
                )
            images[version] = struct.pack(f">{len(code)}I", *code)
            inventories[version] = ff
        return images, inventories

    def test_near_match_chain_cannot_join_dissimilar_endpoints(self):
        images, inventories = self.fixture()
        evidence = {}
        names = correspondence(images, inventories, "de", symbol_policy=SymbolPolicy(0.93, 0.1), evidence=evidence)
        self.assertEqual(len({names[v][12] for v in inventories}), 3)
        self.assertEqual({evidence[v][12] for v in inventories}, {"symbol-leaf-similarity-low"})

    def test_evidence_and_membership_are_independent_of_version_and_row_iteration(self):
        images, inventories = self.fixture()
        details = []
        partitions = []
        for ordered in (inventories, {v: list(reversed(rows)) for v, rows in reversed(list(inventories.items()))}):
            proof = {}
            names = correspondence(images, ordered, "de", symbol_policy=SymbolPolicy(0.9, 0.1), symbol_evidence=proof)
            groups = {}
            for v, rows in names.items():
                for at, name in rows.items():
                    groups.setdefault(name, set()).add((v, at))
            partitions.append({frozenset(rows) for rows in groups.values()})
            details.append(json.dumps(proof, sort_keys=True))
        # Naming follows declared version order; correspondence does not.
        self.assertEqual(partitions[0], partitions[1])
        self.assertEqual(details[0], details[1])

    def test_overlay_name_collisions_are_independent_of_row_iteration(self):
        image = struct.pack(">4I", 0x03E00008, 0, 0x03E00008, 0)
        inventories = {
            v: [Function(v, "func_80001000", at, at + 8, 0x80001000, "func_80001000", "asm", ()) for at in (0, 8)]
            for v in ("us", "eu")
        }
        kwargs = {"symbol_policy": SymbolPolicy(0.9, 0.1)}
        images = dict.fromkeys(inventories, image)
        names = correspondence(images, inventories, "us", **kwargs)
        reversed_rows = {v: list(reversed(rows)) for v, rows in inventories.items()}
        self.assertEqual(names, correspondence(images, reversed_rows, "us", **kwargs))
