"""Small real scheduler/allocator payloads; no compiler or timing probes."""

import json
import struct
import unittest
from pathlib import Path
from unittest.mock import patch

from unbake.compilers.families import family_for
from unbake.compilers.families.gcc import Gcc
from unbake.work import compare_facts
from unbake.work.score import measure_words

FIXTURES = Path(__file__).parent / "fixtures" / "dump_facts"


def dumps(label):
    return {s: (FIXTURES / f"{label}.{s}").read_text() for s in ("sched", "sched2", "dbr", "lreg", "greg")}


def payload(label="baseline", version="us-rev1"):
    return json.loads((FIXTURES / f"A4134-{label}.json").read_text())[version]


def measurement(doc, version="us-rev1"):
    return measure_words(
        version,
        struct.pack(f">{len(doc['target'])}I", *doc["target"]),
        struct.pack(f">{len(doc['candidate'])}I", *doc["candidate"]),
    )


class DumpFactsTests(unittest.TestCase):
    def test_real_first_pass_birth_pair_before_and_winner_and_unique_words(self):
        for label, before in (("A4134-before", True), ("A4134-winner", False)):
            doc = payload("baseline" if before else "B-equalize-scheduler-births")
            parsed = Gcc().compiler_facts(dumps(label), tuple(doc["candidate"]), (FIXTURES / f"{label}.i").read_text())
            ready = next(r for r in parsed["schedule"] if r["pass"] == "sched" and r["step"] == 4)
            self.assertEqual(ready["block"], 12)
            self.assertEqual([r["uid"] for r in ready["ready"]], [134, 139])
            self.assertEqual([r["base_priority"] for r in ready["ready"]], [1, 1])
            self.assertEqual([r["ready_priority"] for r in ready["ready"]], [0x7F000001, 1 if before else 0x7F000001])
            self.assertEqual([r["promotion"] for r in ready["ready"]], ["birth", "none" if before else "birth"])
            self.assertEqual([r["birth"] for r in ready["ready"]], [True, not before])
            self.assertEqual(ready["selected"], [134, 139] if before else [139, 134])
            self.assertTrue(all(len(r["candidate_offsets"]) == 1 for r in ready["ready"]))
            self.assertEqual(parsed["instructions"][139]["in_place"], before)

    def test_stock_priority_is_unavailable_and_hard_conflicts_block_b1520(self):
        parsed = Gcc().compiler_facts(dumps("B1520-before"), (), "")
        pseudo = next(p for p in parsed["pseudos"] if p["pseudo"] == 74)
        self.assertEqual((pseudo["rank"], pseudo["references"], pseudo["live_length"]), (0, 224, 335))
        self.assertEqual(pseudo["candidate_hard"], [16, 17])
        self.assertTrue({4, 5} <= set(pseudo["hard_conflicts"]))
        self.assertEqual(pseudo["priority"], "unavailable")
        # The historical 93611 is a formula estimate, absent from this dump.
        self.assertNotIn(93611, pseudo.values())

    def test_a8a8_hard_f20_conflict_and_actual_early_recoloring(self):
        before = Gcc().compiler_facts(dumps("A8A8-before"), (), "")
        after = Gcc().compiler_facts(dumps("A8A8-split"), (), "")
        a = {p["pseudo"]: p for p in before["pseudos"]}
        b = {p["pseudo"]: p for p in after["pseudos"]}
        self.assertEqual((a[80]["references"], a[80]["live_length"], a[80]["candidate_hard"]), (7, 28, [56]))
        self.assertIn(52, a[80]["hard_conflicts"])
        self.assertEqual(a[80]["priority"], "unavailable")
        self.assertEqual(b[87]["candidate_hard"], [36])
        self.assertNotIn(52, b[87]["hard_conflicts"])
        self.assertEqual((a[77]["candidate_hard"], b[77]["candidate_hard"]), ([54], [56]))
        self.assertEqual((a[82]["candidate_hard"], b[82]["candidate_hard"]), ([36], [38]))

    def test_explicit_ido_unsupported_with_zero_dump_recipe(self):
        for ident in ("ido-5.3", "ido-7.1"):
            family = family_for(ident)
            self.assertEqual(family.compare_dump_flags(), ())
            parsed = family.compiler_facts({}, (), "")
            self.assertFalse(parsed["available"])
            self.assertIn("unsupported", parsed["reason"])

    def test_each_dump_scanned_once_and_reused_across_regions(self):
        from unbake.compilers.families.gcc import dump_facts
        from unbake.work import compare_dump

        doc = payload()
        result = measurement(doc)
        data = compare_facts.facts(result, 0x802A4134, {})
        with patch.object(dump_facts, "records", wraps=dump_facts.records) as scans:
            parsed = Gcc().compiler_facts(
                dumps("A4134-before"), tuple(doc["candidate"]), (FIXTURES / "A4134-before.i").read_text()
            )
            compare_dump.attach_decisions(data, result, parsed)
        self.assertEqual(scans.call_count, 2)  # preallocation and final RTL, not per region
        regions = [r for r in data["regions"] if "compiler_facts" in r]
        self.assertTrue(regions)
        self.assertEqual(sum(len(r["compiler_facts"]["search_options"]) for r in regions), 1)
        self.assertTrue(all(r["compiler_facts"]["row_limit"] == 64 for r in regions))

    def test_duplicate_candidate_words_refuse_unique_mapping_and_search(self):
        from unbake.work import compare_dump

        doc = payload()
        word = doc["candidate"][65]  # mapped shift
        candidate = (*doc["candidate"], word)
        parsed = Gcc().compiler_facts(dumps("A4134-before"), candidate, (FIXTURES / "A4134-before.i").read_text())
        self.assertEqual(parsed["instructions"][134]["candidate_offsets"], [])
        result = measurement({**doc, "candidate": list(candidate)})
        data = compare_facts.facts(result, 0x802A4134, {})
        compare_dump.attach_decisions(data, result, parsed)
        self.assertFalse(any(r.get("compiler_facts", {}).get("search_options") for r in data["regions"]))

    def test_multiple_competing_pairs_are_explicitly_held_instead_of_truncated_to_one(self):
        from unbake.work import compare_dump

        doc = payload()
        result = measurement(doc)
        data = compare_facts.facts(result, 0x802A4134, {})
        parsed = Gcc().compiler_facts(
            dumps("A4134-before"), result.candidate, (FIXTURES / "A4134-before.i").read_text()
        )
        pair = next(r for r in parsed["schedule"] if r["pass"] == "sched" and r["step"] == 4)
        parsed["schedule"].append(pair)
        compare_dump.attach_decisions(data, result, parsed)
        regions = [r["compiler_facts"] for r in data["regions"] if "compiler_facts" in r]
        self.assertTrue(regions)
        self.assertFalse(any(r["search_options"] for r in regions))
        self.assertTrue(any("multiple" in r["search_reason"] for r in regions))

    def test_all_holding_raw_payloads_and_native_assembler_control(self):
        for label, expected in (
            ("baseline", 8),
            ("B-end-selected-before-exit", 4),
            ("B-schedule-tie", 8),
            ("B-equalize-scheduler-births", 0),
        ):
            docs = json.loads((FIXTURES / f"A4134-{label}.json").read_text())
            self.assertEqual(set(docs), {"de", "eu", "eu-x", "us", "us-rev1"})
            for version, doc in docs.items():
                measured = measurement(doc, version)
                self.assertEqual(measured.target_words_different * 4, expected)
                self.assertEqual(measured.inserted_words, 0)
                self.assertEqual(measured.exact, expected == 0)
        for row in json.loads((FIXTURES / "native-control.json").read_text()):
            self.assertTrue(row["same_text_bytes"])
            self.assertTrue(row["same_text_relocations"])

    def test_printed_override_only_and_missing_fields_are_unavailable(self):
        sample = dumps("A4134-before")
        sample["galloc"] = (
            ";; allocno 0 pseudo 88 SI size 1 refs 9 live_length 10 calls 0\n"
            ";; allocno 0 priority log2(refs)*refs/live = 12345\n"
        )
        parsed = Gcc().compiler_facts(sample, (), "")
        self.assertEqual(next(p for p in parsed["pseudos"] if p["pseudo"] == 88)["priority"], 12345)
        parsed = Gcc().compiler_facts({"sched": sample["sched"]}, (), "")
        self.assertEqual(parsed["pseudos"], [])
        self.assertTrue(parsed["limitations"])


if __name__ == "__main__":
    unittest.main()
