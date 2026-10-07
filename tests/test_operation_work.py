"""Public baseline-compatible count regressions for repeated parser work and route-11 words."""

import hashlib
import struct
from types import SimpleNamespace
from unittest.mock import patch

from tests.project_fixture import ProjectCase
from unbake import steps
from unbake.config import Held
from unbake.typemap import declarations
from unbake.work import compare, score, search


class OperationWorkTests(ProjectCase):
    def test_unchanged_parser_refusal_reopens_with_one_total_owner_call(self):
        # Slice of the cycle6 owning declaration failure, whose captured token is func_8025F074_us.
        source = self.project.include[0] / "refused.h"
        source.write_text("UNKNOWN_RETURN func_8025F074_us(void);\n")
        calls = []

        def run(project, host):
            calls.append(1)
            text = source.read_text()
            declarations.extract(
                text, {"kind": "declared", "version": "eu", "sha256": hashlib.sha256(text.encode()).hexdigest()}
            )

        step = steps.Step(
            "types", "types", "parser input changed", lambda p, h: hashlib.sha256(source.read_bytes()).hexdigest(), run
        )
        with patch.object(steps, "STEPS", {"types": step}):
            for _ in range(2):
                with self.assertRaises(Held):
                    steps.ensure(self.project, self.host, ("types",))
        self.assertEqual(len(calls), 1)

    def test_search_names_19_of_20_as_one_different_and_zero_native_calls(self):
        target = struct.pack(">20I", *([0x24420004] * 20))
        candidate = target[:-4] + bytes(4)
        measure = getattr(score, "measure_words", None) or score.compare_words
        measured = {v: measure(v, target, candidate) for v in ("eu-x", "de")}
        file = self.project.work / "alpha" / "alpha.c"
        file.parent.mkdir(parents=True)
        file.write_text("int alpha(void) { return 1; }\n")
        trace = file.with_suffix(".trace")
        trace.write_text("{}\n")
        trial = compare.Compared("alpha", file, hashlib.sha256(file.read_bytes()).hexdigest(), measured)
        result = SimpleNamespace(source=file, trial=trial, trials=1, steps=trace, score=19, fuzzy=95.0, skips=())
        with (
            patch("unbake.search.core.run", return_value=result) as run,
            patch("unbake.search.methods", return_value=[object()]),
            patch("subprocess.run") as native,
        ):
            found = search.search(self.project, self.host, file, "types", 60)
        data = found.document()
        different = data["words"] if "words" in data else data["measurements"]["eu-x"]["target_words_different"]
        self.assertEqual(different, 1)
        self.assertEqual((native.call_count, run.call_count, data["mutations"]), (0, 1, 0))
        self.assertTrue(any("1 target word different" in line for line in found.lines()))
