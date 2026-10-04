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
# Human rendering (stderr only). JSON string fields may hold help or usage text, never rendered receipts.
HUMAN_MARKERS = ("OK(", "HELD(", "FAILED(", "Next:")


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
        for line in stdout.splitlines():
            value = json.loads(line)
            for receipt in value["receipts"]:
                self.assertFalse(receipt.startswith(HUMAN_MARKERS), receipt)
            self.assertNotIn("Next:", json.dumps(value["data"]))

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
        for verb in set(VERBS) - {"recompute"}:
            self.assertIn(verb, stderr)
        for verb in ("try", "submit", "clone", "recompute"):
            self.assertNotRegex(stderr, rf"\b{verb}\b")


class SourcePrintTests(TempCase):
    def test_no_print_writes_stdout(self) -> None:
        """Every print in the tool names its stream; a bare print would corrupt the JSON stdout."""
        import ast
        from pathlib import Path

        import unbake

        bare = []
        for path in sorted(Path(unbake.__file__).parent.rglob("*.py")):
            for node in ast.walk(ast.parse(path.read_text())):
                is_print = isinstance(node, ast.Call) and getattr(node.func, "id", None) == "print"
                if is_print and not any(keyword.arg == "file" for keyword in node.keywords):
                    bare.append(f"{path.name}:{node.lineno}")
        self.assertEqual([], bare)
