"""The cycle narrates a finished search as one nested line: no cache path and no Next."""

import io
import unittest
from contextlib import redirect_stderr

from unbake.cycle import engine


class NarrateSearchTests(unittest.TestCase):
    def test_a_finished_search_is_one_nested_line(self) -> None:
        record = {
            "event": "fn.search.done",
            "function": "f",
            "method": "types",
            "ok": True,
            "seconds": 60.0,
            "mutations": 2,
            "words": 3,
        }
        err = io.StringIO()
        with redirect_stderr(err):
            engine.narrate(record)
        self.assertEqual(err.getvalue(), "  f: tried 2 variants in 60s; best leaves 3 words different\n")
