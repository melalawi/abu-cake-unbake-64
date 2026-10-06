"""A refused native compile cannot start the mechanical byte-difference ladder."""

import hashlib
import io
import json
from unittest.mock import patch

from tests.project_fixture import ProjectCase
from unbake.config import Held
from unbake.cycle import engine
from unbake.cycle.events import Emitter
from unbake.work import compare
from unbake.work.score import Compare


class CompareRefusalTests(ProjectCase):
    def test_mixed_native_failure_preserves_every_version_and_waits_for_a_source_edit(self):
        file = self.project.work / "alpha" / "alpha.c"
        file.parent.mkdir(parents=True)
        file.write_text("int alpha(void) { return *1; }\n")
        sha = hashlib.sha256(file.read_bytes()).hexdigest()
        failure = {
            "chain": [
                {
                    "phase": "compile",
                    "key": "compile.cc1",
                    "reason": "invalid unary *",
                    "fault": {
                        "args": ["cc1", "alpha.i"],
                        "cwd": str(self.root),
                        "exit": 33,
                        "signal": None,
                        "stdout": "",
                        "stderr": "invalid unary *\n",
                    },
                }
            ]
        }
        measured = compare.Compared(
            "alpha",
            file,
            sha,
            {
                "eu": Compare("eu", 1, 3, {"changed": 2}, ["first divergence: +4"], 33.3),
                "us": Compare("us", 0, 3, {"changed": 3}, ["VERSION us: compile.cc1: invalid unary *"], 0.0),
            },
            faults={"us": failure},
        )
        with patch.object(compare, "compare", return_value=measured):
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
        self.assertEqual(event["per_version"]["us"]["fault"], failure)
        self.assertEqual(event["per_version"]["eu"]["percent"], 33.3)
        self.assertIn("invalid unary *", event["diagnostic"])

    def test_exception_refusal_still_waits_without_a_partial_measurement(self):
        with patch.object(compare, "compare", side_effect=Held("compare", "compare.link: unresolved symbol")):
            result = engine._compare_task((self.project.root, self.host, "alpha.c"))
        row = engine.Row("alpha", 12, self.versions, False)
        stream = io.StringIO()
        self.assertEqual(engine._compared(row, result, Emitter(stream), engine.Stop("all-landed")), "")
        self.assertEqual(json.loads(stream.getvalue())["per_version"], {})
