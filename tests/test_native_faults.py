"""Complete typed native transport and the first owning cause survive every wrapper."""

import errno
import subprocess
from types import SimpleNamespace
from unittest.mock import patch

from tests.kit import TempCase
from unbake import process
from unbake.cli.guidance import after
from unbake.cli.output import Result
from unbake.config import Held


class NativeFaultTests(TempCase):
    def test_both_streams_and_exact_context_survive_an_owning_wrapper(self):
        argv = ["cc", "-O3", "unit.c"]
        context = {"source": "src/unit.c", "version": "de", "function": "unit", "address": 0x80001000}
        with (
            patch.object(
                process.subprocess,
                "run",
                return_value=subprocess.CompletedProcess(argv, 3, "stdout detail", "stderr detail"),
            ),
            self.assertRaises(Held) as caught,
        ):
            process.run_tool(argv, self.root, "compile", context=context)
        original = caught.exception.fault
        wrapped = Held(
            process.capture(
                caught.exception,
                cause=process.named("draft.unit", "compiler prerequisite failed", owner="work.draft", stage="draft"),
            )
        )
        result = Result.held("draft", wrapped, "stop: repair owning prerequisite")
        self.assertEqual(result.document()["v"], 2)
        self.assertEqual(wrapped.key, caught.exception.key)
        leaf = process.native_results(wrapped.fault)[0]
        self.assertEqual((leaf.args, leaf.cwd, leaf.exit, leaf.signal), (tuple(argv), str(self.root), 3, None))
        self.assertEqual((leaf.stdout, leaf.stderr, leaf.context), ("stdout detail", "stderr detail", context))
        reopened = process.Fault.read(wrapped.fault.document())
        self.assertEqual(reopened.cause.id, original.cause.id)
        self.assertEqual(process.native_results(reopened), (leaf,))

    def test_same_cause_identity_keeps_distinct_context_and_deduplicates_exact_cause(self):
        cause = process.named("types.declaration", "unclosed body", owner="parser", stage="solve")
        original = Held(cause)
        context = process.named("types.declaration", "src/unit.c: de: unclosed body", owner="parser", stage="solve")
        self.assertEqual(cause.id, context.id)
        wrapped = process.capture(original, cause=context)
        self.assertIs(wrapped.cause, cause)
        self.assertEqual(wrapped.chain[-1].reason, context.reason)
        self.assertIs(process.capture(original, cause=cause), original.fault)

    def test_permission_is_distinct_from_signal_and_exit(self):
        with (
            patch.object(process.subprocess, "run", side_effect=PermissionError(errno.EACCES, "Permission denied")),
            self.assertRaises(Held) as caught,
        ):
            process.run_tool(["cc"], self.root, "compile")
        leaf = process.native_results(caught.exception.fault)[0]
        self.assertEqual((leaf.category, leaf.errno, leaf.exit), ("native-os", errno.EACCES, None))
        with (
            patch.object(process.subprocess, "run", return_value=subprocess.CompletedProcess(["cc"], -9, "out", "err")),
            self.assertRaises(Held) as caught,
        ):
            process.run_tool(["cc"], self.root, "compile")
        leaf = process.native_results(caught.exception.fault)[0]
        self.assertEqual((leaf.exit, leaf.signal, leaf.stderr), (None, 9, "err"))

    def test_guidance_renders_only_the_owning_action(self):
        cause = process.named(
            "compile.missing_provider",
            "basetypes.h missing",
            owner="family",
            stage="preprocess",
            action=process.Action("command", ("compare", "unit.c")),
        )
        wrapper = Held(process.Fault(cause).framed("draft", "draft", "outer diagnostic"))
        context = SimpleNamespace(cmd=lambda *args: "unbake " + " ".join(args))
        self.assertEqual(after(context, wrapper), "unbake compare unit.c")
        self.assertEqual(Result.held("draft", wrapper, after(context, wrapper)).key, "compile.missing_provider")
