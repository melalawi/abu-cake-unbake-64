"""The output contract of every public verb: stdout is JSON only, all human text is on stderr."""

import io
import json
from contextlib import redirect_stderr, redirect_stdout

from tests.kit import TempCase
from unbake.cli.main import main

KEYS = {"v", "command", "status", "key", "data", "next", "receipts"}
VERBS = {
    "init": ["demo", "--functions-per-header", "2"],
    "setup": [],
    "next": [],
    "draft": ["alpha"],
    "compare": ["alpha.c"],
    "tidy": ["alpha.c"],
    "search-variants": ["alpha.c", "--method", "order", "--seconds", "1"],
    "publish": ["alpha.c"],
    "boundary": ["function", "alpha", "--version", "us", "--start", "0", "--end", "4"],
    "check": [],
    "explain": ["alpha"],
    "cycle": ["--stop", "all-landed"],
    "recompute": ["--all"],
}
DELETED = ["try", "submit", "map", "solve", "layout", "report", "collect", "clone", "split", "decomp", "rodata"]
HUMAN_MARKERS = ("OK(", "HELD(", "Next:", "usage:", "Usage:")


class ContractTests(TempCase):
    def run_main(self, argv: list[str]) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            try:
                code = main(argv)
            except SystemExit as stop:
                code = int(stop.code or 0)
        return code, out.getvalue(), err.getvalue()

    def only_object(self, stdout: str) -> dict:
        lines = stdout.splitlines()
        self.assertEqual(len(lines), 1, stdout)
        value = json.loads(lines[0])
        self.assertIsInstance(value, dict)
        self.assertEqual(set(value), KEYS)
        self.assertEqual(value["v"], 1)
        return value

    def assert_stdout_is_json_only(self, stdout: str) -> None:
        for marker in HUMAN_MARKERS:
            self.assertNotIn(marker, stdout)

    def test_help_of_every_verb(self) -> None:
        for verb in VERBS:
            with self.subTest(verb):
                code, stdout, stderr = self.run_main(["--project", str(self.root), verb, "--help"])
                result = self.only_object(stdout)
                self.assertEqual((code, result["status"], result["command"], result["key"]), (0, "ok", "help", None))
                self.assertIn("usage", result["data"])
                self.assertIn("usage", stderr.lower())
                self.assert_stdout_is_json_only(stdout)

    def test_refusal_of_every_verb_is_held_json_with_human_text_on_stderr(self) -> None:
        missing = self.root / "absent.toml"
        for verb, args in VERBS.items():
            with self.subTest(verb):
                argv = ["--project", str(self.root / "no-project"), "--config", str(missing), verb, *args]
                if verb == "init":
                    argv = ["--config", str(missing), verb]
                code, stdout, stderr = self.run_main(argv)
                lines = [json.loads(line) for line in stdout.splitlines()]
                self.assertEqual(len(lines), 1, stdout)
                result = lines[0]
                self.assertEqual(set(result), KEYS)
                self.assertEqual((code, result["status"]), (1, "held"))
                self.assertTrue(result["key"])
                self.assertTrue(result["data"]["reason"].startswith(result["key"]))
                self.assert_stdout_is_json_only(stdout)
                self.assertIn("HELD(", stderr)
                if result["next"] is not None:
                    self.assertRegex(result["next"], r"^(unbake \S|stop: )")
                    self.assertIn("Next: " + result["next"], stderr)

    def test_deleted_verbs_are_usage_refusals(self) -> None:
        for verb in DELETED:
            with self.subTest(verb):
                code, stdout, stderr = self.run_main(["--project", str(self.root), verb])
                result = self.only_object(stdout)
                self.assertEqual((code, result["status"], result["key"]), (1, "held", "usage"))
                self.assert_stdout_is_json_only(stdout)
                self.assertTrue(stderr)

    def test_top_level_help_is_one_object(self) -> None:
        code, stdout, stderr = self.run_main(["--help"])
        result = self.only_object(stdout)
        self.assertEqual((code, result["status"]), (0, "ok"))
        for verb in VERBS:
            self.assertIn(verb, stderr)
        for verb in ("try", "submit", "clone"):
            self.assertNotRegex(stderr, rf"\b{verb}\b")
