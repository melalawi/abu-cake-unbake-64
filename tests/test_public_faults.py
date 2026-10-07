"""Public producers retain native cause data when they turn exceptions into results."""

from concurrent.futures import Future
from unittest.mock import patch

from tests.project_fixture import ProjectCase
from unbake import land, process, runner
from unbake.compilers import candidates, choice
from unbake.config import Held
from unbake.cycle import engine
from unbake.layout import header_step
from unbake.process import named
from unbake.work import compare


class PublicFaultTests(ProjectCase):
    versions = ("us",)

    def native_error(self):
        return Held(
            process.Fault(
                named("compile.cc", "native refusal", owner="fixture", stage="compile"),
                (
                    process.NativeResult(
                        ("/native/cc", "alpha.i"),
                        str(self.root),
                        3,
                        None,
                        "native stdout\ncomplete\n",
                        "native stderr\ncomplete\n",
                        "native-exit",
                        None,
                        "utf-8",
                        "surrogateescape",
                        {"function": "alpha", "version": "us"},
                    ),
                ),
            )
        )

    def source(self):
        path = self.project.src / "alpha.c"
        path.write_text("int alpha(void) { return 1; }\n")
        return path

    def test_compile_score_keeps_native_cause_in_public_version_result(self):
        error = self.native_error()
        with patch.object(runner, "compile_unit", side_effect=error):
            result = compare.measure(self.project, self.host, self.source())
        self.assertEqual(
            process.Fault.read(result.document()["versions"]["us"]["fault"]).cause.id, error.fault.cause.id
        )
        self.assertEqual(
            process.native_results(process.Fault.read(result.document()["versions"]["us"]["fault"])),
            process.native_results(error.fault),
        )
        self.assertFalse(result.exact)

    def test_all_compiler_refusals_retain_their_individual_causes(self):
        error = self.native_error()
        with patch.object(choice, "alternatives", return_value=[]), self.assertRaises(Held) as raised:
            candidates.resolve(self.project, self.host, self.source(), error)
        self.assertEqual(
            process.Fault.read(raised.exception.data["compilers"][self.project.default_compiler]).cause.id,
            error.fault.cause.id,
        )

    def test_publish_failure_document_keeps_the_native_cause(self):
        error = self.native_error()
        with patch.object(land, "land", side_effect=error):
            result = land.publish(self.project, self.host, [self.source()])
        self.assertEqual(
            process.Fault.read(result.document()["failed"]["alpha"]["fault"]).cause.id, error.fault.cause.id
        )
        self.assertEqual(
            process.native_results(process.Fault.read(result.document()["failed"]["alpha"]["fault"])),
            process.native_results(error.fault),
        )

    def test_header_compile_failure_keeps_the_native_cause(self):
        error = self.native_error()
        source = self.source()
        with patch.object(runner, "compile_unit", side_effect=error):
            result = header_step._compile((self.project, self.host, source, "us", "alpha", False, False))
        self.assertEqual(process.Fault.read(result["fault"]).cause.id, error.fault.cause.id)
        self.assertEqual(
            process.native_results(process.Fault.read(result["fault"])), process.native_results(error.fault)
        )

    def test_unexpected_worker_failure_keeps_the_exception_cause_chain(self):
        error = self.native_error()
        future = Future()
        future.set_exception(error)
        result = engine._result(future)
        self.assertEqual(process.Fault.read(result["fault"]).cause.id, error.fault.cause.id)
        self.assertEqual(
            process.native_results(process.Fault.read(result["fault"])), process.native_results(error.fault)
        )
        self.assertEqual(result["key"], error.key)

    def test_make_failure_keeps_native_streams_and_the_clean_build_environment(self):
        import os
        import subprocess

        from unbake import build
        from unbake.project import hygiene

        stdout, stderr = "first diagnostic\n" + "detail\n" * 30, "last diagnostic\n"
        with (
            patch.object(build.steps, "ensure"),
            patch.object(hygiene, "tracked_findings", return_value=[]),
            patch.object(build, "python_visible", return_value=None),
            patch.object(
                process.subprocess, "run", return_value=subprocess.CompletedProcess(["make"], 7, stdout, stderr)
            ) as native,
        ):
            result = build.check(self.project, self.host)
        fault = next(frame["result"] for frame in result.document()["fault"]["chain"] if frame["kind"] == "native")
        self.assertEqual((fault["exit"], fault["stdout"], fault["stderr"]), (7, stdout, stderr))
        self.assertEqual(native.call_args.kwargs["env"]["PATH"], os.pathsep.join(map(str, self.host.tool_path)))
        self.assertNotIn("PYTHONPATH", native.call_args.kwargs["env"])

    def test_extraction_failure_keeps_separate_complete_native_streams(self):
        import subprocess

        from unbake import extract

        def failed(argv, **named):
            stdout, stderr = "splat output\n", "splat refusal\n"
            if not named.get("text"):
                stdout, stderr = stdout.encode(), stderr.encode()
            return subprocess.CompletedProcess(argv, 5, stdout, stderr)

        with patch.object(process.subprocess, "run", side_effect=failed), self.assertRaises(Held) as caught:
            extract._make_archive(self.project, self.host, "us", self.root / "extract.tar")
        self.assertEqual(caught.exception.fault.document()["chain"][0]["result"]["exit"], 5)
        self.assertEqual(caught.exception.fault.document()["chain"][0]["result"]["stdout"], "splat output\n")
        self.assertEqual(caught.exception.fault.document()["chain"][0]["result"]["stderr"], "splat refusal\n")

    def test_exact_admission_keeps_the_unavailable_compare_native_cause(self):
        import hashlib

        from tests.ledger_fixture import log_attempt
        from unbake.work import attempts
        from unbake.work.score import unavailable

        source = self.source()
        error = self.native_error()
        measured = unavailable("us", 3, error.fault).document()
        log_attempt(
            self.project,
            attempts.Attempt(
                attempts.now(),
                "alpha",
                hashlib.sha256(source.read_bytes()).hexdigest(),
                12,
                {"us": measured},
                None,
                False,
                0.0,
                self.project.default_compiler,
            ),
        )
        with patch("subprocess.run") as native, self.assertRaises(Held) as caught:
            land.exact_attempt(self.project, "alpha", source)
        self.assertEqual(caught.exception.key, error.key)
        self.assertEqual(caught.exception.fault.cause.id, error.fault.cause.id)
        self.assertIn("native_artifact", caught.exception.fault.cause.evidence)
        self.assertEqual(native.call_count, 0)
        self.assertTrue(
            any(frame.reason.endswith("exact comparison unavailable") for frame in caught.exception.fault.chain)
        )
