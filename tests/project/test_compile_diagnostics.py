"""Process failures expose stderr, including a complete preprocessing diagnostic."""

from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

from unbake.project import build
from unbake.project.config import Held
from unbake.project_tools import compile as compiler


class CompileDiagnosticTests(TestCase):
    def test_failure_prefers_stderr_and_preserves_all_lines(self):
        for output, diagnostic in (
            (b"typedef int stdout_noise;\n", b"source.c:12: fatal error\nmissing shared/x.h\n"),
            (b"compiler failure\n", b""),
        ):
            for runner, error in ((build._run, Held), (compiler.run, ValueError)):
                with self.subTest(runner=runner, diagnostic=diagnostic):
                    result = SimpleNamespace(returncode=1, stdout=output, stderr=diagnostic)
                    module = build if runner is build._run else compiler
                    with (
                        patch.object(module.subprocess, "run", return_value=result),
                        self.assertRaises(error) as caught,
                    ):
                        if runner is build._run:
                            runner(["cpp"], Path("/project"))
                        else:
                            runner(["cpp"])
                    message = str(caught.exception)
                    self.assertIn((diagnostic or output).decode(), message)
                    if diagnostic:
                        self.assertNotIn("stdout_noise", message)

    def test_success_returns_preprocessed_stdout(self):
        for runner in (build._run, compiler.run):
            with self.subTest(runner=runner):
                result = SimpleNamespace(returncode=0, stdout=b"typedef int word;", stderr=b"warning")
                module = build if runner is build._run else compiler
                with patch.object(module.subprocess, "run", return_value=result):
                    actual = runner(["cpp"], Path("/project")) if runner is build._run else runner(["cpp"])
                self.assertEqual(actual, result.stdout)


class BatchDiagnosticTests(TestCase):
    def test_data_symbol_hold_retains_location_and_multiline_message(self):
        import tempfile

        from unbake.match import batch

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            symbols = root / "symbols.txt"
            symbols.write_text("")
            project = SimpleNamespace(
                versions=("us",),
                work=root / "work",
                src=root / "src",
                version=lambda _: SimpleNamespace(symbols=symbols),
            )
            policy = SimpleNamespace(cores=1)
            candidate = batch.Candidate(
                "alpha",
                root / "alpha.c",
                b"",
                "",
                ("us",),
                True,
                final="extern int missing;\nint alpha(void) {return missing;}\n",
            )
            diagnostic = "cpp exited 1: " + "long/path/" * 45 + "source.c:12\n fatal error: shared/old.h missing\n"
            receipts = []
            with (
                patch.object(batch.build, "compile_objects", return_value={"alpha": diagnostic}),
                patch.object(batch.data_symbols, "resolve", return_value=[]),
            ):
                accepted = batch._data_symbols(project, policy, [candidate], receipts)
            self.assertEqual(accepted, [])
            self.assertEqual(len(receipts), 1)
            self.assertIn(diagnostic.strip(), receipts[0])
