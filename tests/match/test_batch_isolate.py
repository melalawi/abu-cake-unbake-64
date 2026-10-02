"""Fallback isolation gathers independent faults without blaming a broken base."""

import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from unbake.match.batch import Candidate, _bisect
from unbake.project.config import Held


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
