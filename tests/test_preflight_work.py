"""Public preflight uses the recorded RW macro lines and a real three-function layout.

Provenance: RW-cycle2 runtime-split-eux-apply.err line 3 (NULL at 77),
RW-A boundary-80439F10-apply.log line 3 (SCREEN_WD/SCREEN_HT at 46/47).
Only make/freshness seams are replaced; rules, layout, files and CLI are real.
"""

import io
import json
import subprocess
from contextlib import nullcontext, redirect_stderr, redirect_stdout
from unittest.mock import patch

from tests.project_fixture import ProjectCase
from unbake import admission, build, buildfiles, config, process, steps
from unbake.cli import main
from unbake.decomp import checks
from unbake.project import hygiene


class PreflightWorkTests(ProjectCase):
    versions = ("us",)

    def source(self, name="func_802A03AC_de", line=77, text="#define NULL ((void *)0)"):
        path = self.project.src / (name + ".c")
        path.write_text("\n" * (line - 1) + text + "\n")
        return path

    def invoke(self, command):
        out, err = io.StringIO(), io.StringIO()
        with (
            redirect_stdout(out),
            redirect_stderr(err),
            patch.object(config, "load_host", return_value=self.host),
            patch.object(admission, "command", return_value=nullcontext()),
            patch.object(buildfiles, "generate", return_value={}),
            patch.object(buildfiles, "write", return_value=[]),
            patch.object(hygiene, "tracked_findings", return_value=[]),
            patch.object(build, "python_visible", return_value=None),
            patch.object(steps, "ensure", return_value=[]) as fresh,
            patch.object(
                process, "run_native", return_value=subprocess.CompletedProcess(["make"], 0, "", "")
            ) as native,
            patch("unbake.atomic.text", wraps=__import__("unbake.atomic", fromlist=["text"]).text) as installed,
            patch.object(checks, "run", wraps=checks.run) as scanned,
        ):
            main.main(["--project", str(self.project.root), *command])
        return (
            json.loads(out.getvalue()),
            err.getvalue(),
            (scanned.call_count, fresh.call_count, native.call_count, installed.call_count),
        )

    def boundary(self):
        return self.invoke(
            ["boundary", "function", "tiny", "--version", "us", "--start", "0x44", "--end", "0x48", "--apply"]
        )

    def test_boundary_known_macro_does_no_build_step_or_install(self):
        source = self.source()
        original = self.project.version("us").split.read_bytes()
        result, stderr, counts = self.boundary()
        # Count first: the baseline must fail on executed work, never on a new API/field.
        self.assertEqual(counts, (1, 0, 0, 0))
        self.assertEqual(self.project.version("us").split.read_bytes(), original)
        self.assertEqual(result["key"], "check.source_rules")
        data = result["data"]
        self.assertFalse(data["built"])
        self.assertTrue(data["blocked_before_build"])
        self.assertEqual(data["work"], {"source_scans": 1, "step_runs": 0, "make_invocations": 0})
        self.assertEqual(
            [(r["path"], r["line"], r["rule"], r["predates_edit"]) for r in data["findings"]],
            [(str(source.relative_to(self.project.root)), 77, "local-define", True)],
        )
        expected = f"stop: fix local-define at src/{source.name}:77, then run {data['resume_command']}"
        self.assertEqual(result["next"], expected)
        self.assertIn("Next: " + expected, stderr)
        self.assertIn("present before edit; build not started", stderr)
        repeated, _, counts = self.boundary()
        self.assertEqual(counts, (0, 0, 0, 0))
        self.assertTrue(repeated["data"]["reused"])
        unrelated = self.project.src / "aaa_independent.c"
        unrelated.write_text("int independent(void) { return 3; }\n")
        changed, _, counts = self.boundary()
        # Even an earlier new source does not unlock the cached macro refusal.
        self.assertEqual(counts, (0, 0, 0, 0))
        self.assertEqual(changed["key"], "check.source_rules")
        source.write_text("int value(void) { return 0; }\n")
        passed, _, counts = self.boundary()
        self.assertEqual(counts, (2, 1, 1, 2))
        self.assertEqual(passed["status"], "ok")
        self.assertNotEqual(self.project.version("us").split.read_bytes(), original)

    def test_two_real_macro_lines_scan_one_content_once(self):
        self.source("func_80236F1C_de", 46, "#define SCREEN_WD D_800DE880_de\n#define SCREEN_HT D_800DE884_de")
        result, _, counts = self.boundary()
        self.assertEqual(counts, (1, 0, 0, 0))
        self.assertEqual([r["line"] for r in result["data"]["findings"]], [46, 47])

    def test_check_rejects_rules_before_buildfiles_and_make(self):
        self.source()
        result, _, counts = self.invoke(["check"])
        self.assertEqual(counts, (1, 0, 0, 0))
        self.assertFalse(result["data"]["built"])
        self.assertEqual(result["key"], "check.source_rules")

    def test_prepared_rejects_source_change_before_any_install(self):
        source = self.source(text="int value(void) { return 0; }")
        # Public boundary proof rechecks immediately before mutation.
        original = steps.prepare

        def changed(*args, **kwargs):
            prepared = original(*args, **kwargs)
            source.write_text("int value(void) { return 1; }\n")
            return prepared

        with patch.object(steps, "prepare", side_effect=changed):
            result, _, counts = self.boundary()
        self.assertEqual(counts[1:], (0, 0, 0))
        self.assertEqual(result["key"], "prepare.changed")

    def test_proposed_fix_can_remove_existing_gate_but_not_other_blockers(self):
        source = self.source()
        request = steps.PrepareRequest(
            "boundary", (), proposed={source: "int value(void) { return 0; }\n"}, project_scope=True
        )
        prepared = steps.prepare(self.project, self.host, request)
        self.assertEqual(prepared.blocked, ())
        self.assertEqual(prepared.findings.source_scans, 2)
        other = self.source("other", 46)
        from unbake.config import Held

        with self.assertRaises(Held) as held:
            steps.prepare(self.project, self.host, request)
        self.assertEqual(held.exception.data["findings"][0]["path"], "src/" + other.name)

    def test_complete_findings_preserve_all_rows_and_order_across_cache_reuse(self):
        from unbake.cache import Cache

        later = self.source("later", text="#define LATER 2")
        store = Cache(self.project.build / "cache")
        checks.findings(self.project, (later,), store)
        earlier = self.source("earlier", text="#define EARLIER 1")
        first = checks.findings(self.project, (earlier, later), store)
        repeated = checks.findings(self.project, (later, earlier), store)
        self.assertEqual(first.rows, repeated.rows)
        self.assertEqual([row.path for row in first.rows], ["src/earlier.c", "src/later.c"])
        self.assertEqual((first.source_scans, repeated.source_scans), (1, 0))
        self.assertEqual(first.dependency_hashes, repeated.dependency_hashes)
