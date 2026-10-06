"""Shared-template reuse preserves exact rank, receipt order and conflict graph facts."""

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from unbake.typemap import declarations, solver


class BulkReceiptTests(unittest.TestCase):
    def test_intermediate_higher_rank_and_last_equal_receipt_are_preserved(self):
        aliases = {"T": "int"}
        template = {"S": {"size": 4, "alignment": 4, "fields": []}}
        seeds = [
            {"structs": declarations.ProvenStructs(template, {"kind": kind, "source": source}), "aliases": aliases}
            for kind, source in (("declared", "first"), ("proven", "higher"), ("declared", "lower"), ("proven", "last"))
        ]
        graph = SimpleNamespace(facts=[])
        with patch.object(solver, "_comparable", wraps=solver._comparable) as compared:
            found = solver._merge_records(seeds, "structs", graph)
        self.assertEqual(found["S"]["provenance"], {"kind": "proven", "source": "last"})
        self.assertEqual(compared.call_count, 1)
        self.assertEqual(graph.facts, [])

    def test_deferred_receipt_is_flushed_before_a_conflicting_full_path(self):
        aliases = {}
        template = {"S": {"size": 4, "alignment": 4, "fields": []}}
        seeds = [
            {"structs": declarations.ProvenStructs(template, {"kind": "proven", "source": source}), "aliases": aliases}
            for source in ("first", "last")
        ]
        seeds.append(
            {
                "aliases": aliases,
                "structs": {
                    "S": {
                        "size": 8,
                        "alignment": 4,
                        "fields": [],
                        "provenance": {"kind": "machine", "source": "conflict"},
                    }
                },
            }
        )
        graph = SimpleNamespace(facts=[])
        found = solver._merge_records(seeds, "structs", graph)
        self.assertEqual(found["S"]["provenance"]["source"], "last")
        self.assertEqual(
            [(row["kind"], row["entity"], row["provenance"]["source"]) for row in graph.facts],
            [("published_contract_conflict", "structs:S", "conflict")],
        )
        self.assertEqual(graph.facts[0]["previous"]["size"], 4)
