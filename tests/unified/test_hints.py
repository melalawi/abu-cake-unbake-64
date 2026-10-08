"""Retained words and explicitly labelled symptom counterfactuals; no native work."""

import hashlib
import json
import struct
from importlib.resources import files
from unittest.mock import patch

from tests.kit import TempCase
from tests.unified.support import retained
from unbake.work.compare_facts import facts
from unbake.work.hints import proven_techniques, technique_lines
from unbake.work.score import measure_words


class HintTests(TempCase):
    def matched(self, target, candidate):
        measured = measure_words("us", target, candidate)
        observed = facts(measured, 0x800B1520, {})
        observed["source_sha256"] = "counterfactual-source"
        return observed, proven_techniques({"us": observed})

    def test_retained_catalogue_preserves_variants_and_separates_generic_advice_from_evidence(self):
        catalog = json.loads(files("unbake.work").joinpath("crack_hints.json").read_text())
        rows = catalog["hints"]
        self.assertEqual(len(rows), catalog["origin"]["rows"])
        variants = [r["variant"] for r in rows]
        self.assertEqual(len(set(variants)), len(rows))
        duplicated = [r for r in rows if r["id"] == "duplicated-block-not-goto"]
        self.assertEqual(len(duplicated), 2)
        self.assertNotEqual(duplicated[0]["evidence"], duplicated[1]["evidence"])
        for row in rows:
            self.assertEqual(
                row["variant"],
                hashlib.sha256(
                    json.dumps(
                        {key: row[key] for key in ("id", "symptom", "detect", "fix", "evidence")}, sort_keys=True
                    ).encode()
                ).hexdigest(),
            )
            self.assertNotIn("func_", row["fix"])
            self.assertNotIn("BattleTanx", row["fix"])
            self.assertNotIn("Rage Wars", row["fix"])

    def test_real_bt_and_rw_symptoms_retain_counts_and_unknowns_without_diagnostic_compiles(self):
        with patch("unbake.compilers.drivers.run_preprocess", side_effect=AssertionError("no native hints")):
            for label, target, candidate in (
                ("bt", "bt-target.bin", "bt-baseline.bin"),
                ("rw", "rw-target.bin", "rw-plateau.bin"),
            ):
                with self.subTest(retained=label):
                    measured = measure_words("us", retained(target), retained(candidate))
                    observed = facts(measured, 0x800B1520, {})
                    self.assertEqual(observed["symptoms"]["size_delta_bytes"], 0)
                    self.assertEqual(observed["symptoms"]["typed_register"], measured.typed["register"])
                    self.assertEqual(observed["symptoms"]["typed_immediate"], measured.typed["immediate"])
                    self.assertEqual(observed["work_counts"]["native"], 0)
                    proven_techniques({"us": observed})
            self.assertFalse(proven_techniques({"us": {"symptoms": {"sp_offset_only": None, "frame_delta_bytes": -4}}}))

    def test_counterfactual_sp_offsets_and_literal_four_match_only_the_supported_hint(self):
        words = struct.unpack(">" + "I" * (len(retained("bt-target.bin")) // 4), retained("bt-target.bin"))
        frame = next(w for w in words if w >> 16 == 0x27BD and w & 32768)
        store = next(w for w in words if w >> 26 == 0x2B and (w >> 21) & 31 == 29)
        observed, hints = self.matched(struct.pack(">II", frame, store), struct.pack(">II", frame + 4, store + 4))
        self.assertTrue(observed["symptoms"]["sp_offset_only"])
        self.assertEqual(observed["symptoms"]["frame_delta_bytes"], -4)
        self.assertEqual({h["id"] for h in hints}, {"frame-size-unused-locals"})
        literal = next(w for w in words if w >> 26 in {0x23, 0x31, 0x35} and (w >> 21) & 31 != 29 and w & 65535 < 65531)
        _, hints = self.matched(struct.pack(">I", literal), struct.pack(">I", literal + 4))
        self.assertEqual({h["id"] for h in hints}, {"rodata-align-final-address"})
        self.assertTrue(all(line.startswith("this helped before: ") for line in technique_lines(hints)))
        self.assertEqual(hints[0]["authority"], "advisory")

    def test_counterfactual_repeated_deleted_blocks_keep_both_catalogue_variants_and_plateau_is_attempt_local(self):
        words = struct.unpack(">" + "I" * (len(retained("bt-target.bin")) // 4), retained("bt-target.bin"))
        run = words[4:7]
        target = (*run, 0x11223344, *run, 0x55667788)
        candidate = (0x11223344, 0x55667788)
        observed, hints = self.matched(struct.pack(">8I", *target), struct.pack(">2I", *candidate))
        self.assertEqual(observed["symptoms"]["repeated_deleted_runs"], 2)
        duplicate_hints = [h for h in hints if h["id"] == "duplicated-block-not-goto"]
        self.assertEqual(len(duplicate_hints), 2)
        self.assertFalse(any(h["id"] == "compiler-option-plateau" for h in hints))
        plateau = proven_techniques({"us": observed}, 300)
        self.assertTrue(any(h["id"] == "compiler-option-plateau" for h in plateau))
        self.assertFalse(any(h["id"] == "compiler-option-plateau" for h in proven_techniques({"us": observed})))
