"""A refused native compile cannot start the mechanical byte-difference ladder."""

import hashlib
import io
import json
from unittest.mock import patch

from tests.project_fixture import ProjectCase
from unbake.config import Held
from unbake.cycle import engine
from unbake.cycle.events import Emitter
from unbake.process import named
from unbake.work import compare
from unbake.work.score import Measurement


class CompareRefusalTests(ProjectCase):
    def test_mixed_native_failure_preserves_every_version_and_waits_for_a_source_edit(self):
        file = self.project.work / "alpha" / "alpha.c"
        file.parent.mkdir(parents=True)
        file.write_text("int alpha(void) { return *1; }\n")
        sha = hashlib.sha256(file.read_bytes()).hexdigest()
        from unbake.process import Fault, NativeResult

        native = NativeResult(
            ("cc1", "alpha.i"),
            str(self.root),
            33,
            None,
            "",
            "invalid unary *\n",
            "native-exit",
            None,
            "utf-8",
            "surrogateescape",
            {"version": "us"},
        )
        failure = Fault(named("compile.cc1", "invalid unary *", owner="family", stage="compile"), (native,)).document()
        measured = compare.Compared(
            "alpha",
            file,
            sha,
            {
                "eu": Measurement(
                    "eu",
                    True,
                    1,
                    3,
                    (3) + ({"changed": 2}).get("inserted", 0) - ({"changed": 2}).get("missing", 0),
                    (3) - (1),
                    ({"changed": 2}).get("inserted", 0),
                    {"changed": 2},
                    33.3,
                    None,
                    ["first divergence: +4"],
                ),
                "us": __import__("unbake.work.score", fromlist=["unavailable"]).unavailable(
                    "us",
                    3,
                    __import__("unbake.process", fromlist=["Fault"]).Fault(
                        named("compile.cc1", "invalid unary *", owner="family", stage="compile")
                    ),
                ),
            },
            faults={"us": failure},
        )
        with (
            patch.object(compare, "compare", return_value=measured),
            patch.object(compare, "row_of", return_value=__import__("types").SimpleNamespace(start=0, end=12)),
        ):
            result = engine._compare_task((self.project.root, self.host, str(file)))
        row = engine.Row("alpha", 12, self.versions, True, sha256=sha, best_percent=50.0)
        stream = io.StringIO()
        outcome = engine._compared(row, result, Emitter(stream), engine.Stop("all-landed"))
        self.assertEqual(outcome, "")
        self.assertEqual(row.stage, "waiting for edit")
        self.assertIsNone(row.best_percent)
        event = json.loads(stream.getvalue())
        self.assertIsNone(event["best_percent"])
        self.assertEqual(set(event["per_version"]), {"us", "eu"})
        self.assertEqual(Fault.read(event["per_version"]["us"]["fault"]), Fault.read(failure))
        self.assertEqual(event["per_version"]["eu"]["percent"], 33.3)
        self.assertIsNone(event["per_version"]["us"]["percent"])
        self.assertIn("invalid unary *", event["diagnostic"])

    def test_exception_refusal_still_waits_without_a_partial_measurement(self):
        with patch.object(
            compare,
            "compare",
            side_effect=Held(
                named("compare.link", "compare.link: unresolved symbol", owner="fixture", stage="compare")
            ),
        ):
            result = engine._compare_task((self.project.root, self.host, "alpha.c"))
        row = engine.Row("alpha", 12, self.versions, False)
        stream = io.StringIO()
        self.assertEqual(engine._compared(row, result, Emitter(stream), engine.Stop("all-landed")), "")
        self.assertEqual(json.loads(stream.getvalue())["per_version"], {})
