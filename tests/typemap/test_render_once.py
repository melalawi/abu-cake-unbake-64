"""The headers step reuses the types step's render of the stored solution, so generated headers do not churn."""

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from unbake.cache import Cache
from unbake.typemap import regeneration


def session(cache: Cache, inputs: str) -> regeneration.Session:
    made = object.__new__(regeneration.Session)
    made.project = SimpleNamespace(include=(Path("/include"),), root=Path("/"))
    made.cache, made.inputs = cache, hashlib.sha256(inputs.encode()).hexdigest()
    made.reserved, made.consumer_names, made.consumer_tags = set(), {}, {}
    return made


def solution() -> dict:
    value = {kind: {} for kind in ("structs", "functions", "globals", "arrays")}
    value["functions"]["f"] = {"state": "known", "prototype": "void f(void);"}
    value["declaration_evidence"] = {}
    return value


class RenderOnceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.cache = Cache(Path(tempfile.mkdtemp()))
        self.patch = patch.object(regeneration.layout_index, "load", return_value={"headers": {}})
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def render(self, inputs: str, value: dict, header: bytes) -> tuple[dict, int]:
        calls = []

        def compute() -> dict:
            calls.append(1)
            value["declaration_evidence"]["e.h"] = "struct S { int a; };"  # rendering records evidence
            value["declaration_headers"], value["shared_aliases"] = {"f": "a.h"}, {}
            return {Path("/include/a.h"): header}

        return session(self.cache, inputs).render(value, compute), len(calls)

    def test_stored_solution_reuses_the_render(self) -> None:
        published = solution()
        first, computed = self.render("inputs", published, b"extern void f(void);\n")
        self.assertEqual(computed, 1)
        stored = json.loads(json.dumps(published))  # what the types database hands the headers step
        second, computed = self.render("inputs", stored, b"a different render\n")
        self.assertEqual((second, computed), (first, 0))

    def test_changed_inputs_or_solution_render_again(self) -> None:
        self.render("inputs", solution(), b"one\n")
        for label, inputs, change in [
            ("negative: the sources changed", "other inputs", lambda value: None),
            (
                "near miss: one prototype changed",
                "inputs",
                lambda value: value["functions"]["f"].update(prototype="int f(void);"),
            ),
        ]:
            with self.subTest(label):
                value = solution()
                change(value)
                outputs, computed = self.render(inputs, value, b"two\n")
                self.assertEqual((outputs, computed), ({Path("/include/a.h"): b"two\n"}, 1))


if __name__ == "__main__":
    unittest.main()
