from unittest.mock import Mock, patch

from tests.cli.support import MainCase


class MatchReportTests(MainCase):
    def setUp(self) -> None:
        super().setUp()
        guidance = patch("unbake.cli.guidance.resolve", return_value="unbake next")
        guidance.start()
        self.addCleanup(guidance.stop)

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

    def test_report_dispatch(self) -> None:
        report_path = self.root / "data/report/us.json"
        write = Mock(return_value=[report_path])
        code, out, _error = self.run_main(self.args("report"), {"report": self.module("report", write=write)})
        write.assert_called_once_with(self.project, self.policy)
        self.assertEqual(code, 0)
        self.assertEqual(out.split("Next: ", 1)[0], f"OK(report): {report_path}\n")
