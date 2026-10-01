from unittest.mock import Mock

from tests.cli.support import MainCase


class MatchReportTests(MainCase):
    def test_check_walks_nested_sources_and_retains_marked_findings(self) -> None:
        nested = self.project.src / "nested"
        nested.mkdir()
        bad = nested / "bad.c"
        bad.write_text('void bad(void) { asm("nop"); }\n')
        code, out, error = self.run_main(
            self.args("check"), {"report": self.module("report", findings=Mock(return_value=[]))}
        )
        self.assertEqual(code, 1, error)
        self.assertIn("src/nested/bad.c:1:", out)
        bad.write_text('/* FAKEMATCH: measured instruction required */\nvoid bad(void) { asm("nop"); }\n')
        code, out, error = self.run_main(
            self.args("check"), {"report": self.module("report", findings=Mock(return_value=[]))}
        )
        self.assertEqual(code, 0, error)
        self.assertIn("FAKEMATCH: measured instruction required", out)

    def test_match_submit_withdraw_and_status_signatures(self) -> None:
        submit, withdraw = (Mock(), Mock())
        status = Mock(return_value=["queued alpha"])
        module = self.module("match", submit=submit, withdraw=withdraw, status=status)
        code, out, _error = self.run_main(self.args("match", "submit", str(self.source)), {"match": module})
        submit.assert_called_once_with(self.project, self.policy, self.source)
        self.assertEqual(code, 0)
        code, out, _error = self.run_main(self.args("match", "withdraw", "alpha"), {"match": module})
        withdraw.assert_called_once_with("alpha", project=self.project)
        self.assertEqual(code, 0)
        code, out, _error = self.run_main(self.args("match", "status"), {"match": module})
        status.assert_called_once_with(project=self.project)
        self.assertEqual(code, 0)
        self.assertEqual(out, "OK(match): queued alpha\n")

    def test_match_mixed_receipts_return_failure_without_double_prefix(self) -> None:
        run = Mock(return_value=["OK(match): alpha", "HELD(match): beta: compare failed"])
        code, out, error = self.run_main(self.args("match", "run"), {"match": self.module("match", run=run)})
        run.assert_called_once_with(self.project, self.policy)
        self.assertEqual(code, 1)
        self.assertEqual(out, "OK(match): alpha\nHELD(match): beta: compare failed\n")
        self.assertEqual(error, "")

    def test_report_dispatch(self) -> None:
        report_path = self.root / "data/report/us.json"
        write = Mock(return_value=[report_path])
        code, out, _error = self.run_main(self.args("report"), {"report": self.module("report", write=write)})
        write.assert_called_once_with(self.project, self.policy)
        self.assertEqual(code, 0)
        self.assertEqual(out, f"OK(report): {report_path}\n")

    def test_empty_receipts_still_print_success(self) -> None:
        module = self.module("match", status=Mock(return_value=[]))
        code, out, _error = self.run_main(self.args("match", "status"), {"match": module})
        self.assertEqual(code, 0)
        self.assertEqual(out, "OK(match): no entries\n")
