"""Fallback isolation gathers independent faults without blaming a broken base."""

import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from unbake.match.batch import Candidate, _bisect
from unbake.config import Held


class BisectTests(unittest.TestCase):
    def _candidates(self):
        return [
            Candidate(name, Path(name + ".c"), b"", "", ("us",), True) for name in ("alpha", "beta", "gamma", "delta")
        ]

    def test_all_independent_faults_are_collected(self):
        active = set()

        def materialize(staged, base, members):
            active.clear()
            active.update(member.function for member in members)

        def relink(*args):
            return {"us": SimpleNamespace(ok=not (active & {"alpha", "delta"}))}

        with patch("unbake.match.batch._materialize", materialize), patch("unbake.match.batch._relink", relink):
            faults = _bisect(None, None, None, self._candidates(), {}, ["us"], {})
        self.assertEqual(set(faults), {"alpha", "delta"})

    def test_conflict_holds_only_its_participants(self):
        active = set()

        def materialize(staged, base, members):
            active.clear()
            active.update(member.function for member in members)

        def relink(*args):
            return {"us": SimpleNamespace(ok=not {"alpha", "delta"} <= active)}

        with patch("unbake.match.batch._materialize", materialize), patch("unbake.match.batch._relink", relink):
            faults = _bisect(None, None, None, self._candidates(), {}, ["us"], {})
        self.assertEqual(set(faults), {"alpha", "delta"})

    def test_broken_base_is_not_attributed_to_a_batch_source(self):
        with (
            patch("unbake.match.batch._materialize"),
            patch("unbake.match.batch._relink", return_value={"us": SimpleNamespace(ok=False)}),
            self.assertRaises(Held) as caught,
        ):
            _bisect(None, None, None, self._candidates(), {}, ["us"], {})
        self.assertIn("with no batch sources", caught.exception.reason)


class ReportIsolationTests(unittest.TestCase):
    def test_report_fault_holds_owner_and_publishes_every_remaining_source(self):
        from unbake.match import batch

        for bad, folded, all_bad in [("alpha", False, False), ("secondary", True, False), ("alpha", False, True)]:
            with self.subTest(bad=bad, folded=folded, all_bad=all_bad):
                members = [
                    batch.Candidate(name, Path(name + ".c"), b"", "", ("us",), True)
                    for name in ("alpha", "beta", "gamma")
                ]
                if all_bad:
                    members = members[:1]
                if folded:
                    members[0].removed_rows = {"us": ("      - [0x1000, asm, secondary]\n",)}
                calls = []

                def commit(project, policy, staged, candidates, current, generations, started, calls=calls, bad=bad):
                    calls.append([c.function for c in candidates])
                    if "alpha" in calls[-1]:
                        raise Held("report", f"matched function {bad}: one linked definition is required")
                    return ["followup"]

                project = SimpleNamespace(version=lambda v: SimpleNamespace(split=Mock(read_text=lambda: "split")))
                receipts = []
                with (
                    patch.object(batch, "_commit", side_effect=commit),
                    patch.object(batch, "_materialize") as materialize,
                    patch.object(batch, "_relink", return_value={"us": SimpleNamespace(ok=True)}) as relink,
                    patch.object(
                        batch,
                        "_isolate",
                        side_effect=lambda p, s, b, policy, c, *args, **kwargs: (c, {"us": "new SHA1"}),
                    ) as isolate,
                ):
                    accepted, sha1, followups = batch._publish_survivors(
                        None,
                        project,
                        None,
                        None,
                        members,
                        {},
                        {"us": Path("generation")},
                        {},
                        receipts,
                        {"us": "old SHA1"},
                    )
                self.assertEqual([c.function for c in accepted], [] if all_bad else ["beta", "gamma"])
                self.assertEqual(len(receipts), 1)
                self.assertIn("HELD(report): alpha:", receipts[0])
                self.assertEqual(materialize.call_count, int(not all_bad))
                self.assertEqual(relink.call_count, int(not all_bad))
                self.assertEqual(isolate.call_count, int(not all_bad))
                self.assertEqual(followups, [] if all_bad else ["followup"])
                if not all_bad:
                    self.assertEqual(sha1, {"us": "new SHA1"})
                    self.assertEqual(calls[-1], ["beta", "gamma"])

    def test_unattributable_report_or_other_failure_is_not_blamed_on_a_source(self):
        from unbake.match import batch

        for phase, reason in [
            ("report", "VERSION us: exactly one linked ELF is required"),
            ("report", "matched function baseline: linked ELF is missing"),
            ("match", "matched function alpha: input changed"),
        ]:
            with self.subTest(phase=phase, reason=reason):
                members = [batch.Candidate("alpha", Path("alpha.c"), b"", "", ("us",), True)]
                receipts = []
                with patch.object(batch, "_commit", side_effect=Held(phase, reason)), self.assertRaises(Held):
                    batch._publish_survivors(None, None, None, None, members, {}, {}, {}, receipts, {})
                self.assertEqual(receipts, [])
