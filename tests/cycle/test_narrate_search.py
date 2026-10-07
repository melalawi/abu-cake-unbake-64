"""The cycle narrates a finished search as one nested line: no cache path and no Next."""

import io
import unittest
from contextlib import redirect_stderr

from unbake.cycle import engine
from unbake.work.score import measure_words


class NarrateSearchTests(unittest.TestCase):
    def test_a_finished_search_is_one_nested_line(self) -> None:
        record = {
            "event": "fn.search.done",
            "function": "f",
            "method": "types",
            "ok": True,
            "seconds": 60.0,
            "mutations": 2,
            "measurements": {
                "eu-x": measure_words(
                    "eu-x", bytes.fromhex("24420004") * 20, bytes.fromhex("24420004") * 19 + bytes(4)
                ).document()
            },
        }
        err = io.StringIO()
        with redirect_stderr(err):
            engine.narrate(record)
        self.assertEqual(
            err.getvalue(),
            "f: tried 2 variants\nf: eu-x: 19/20 words identical; 1 target word different; 0 inserted words\n",
        )
