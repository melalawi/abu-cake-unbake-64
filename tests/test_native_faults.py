"""The process owner retains complete native evidence through current Result JSON."""
import errno
import subprocess
from unittest.mock import patch
from tests.kit import TempCase
from unbake import process
from unbake.cli.output import Result
from unbake.config import Held


class NativeFaultTests(TempCase):
    def test_both_streams_and_exact_context_survive_an_owning_wrapper(self):
        argv = ["cc", "-O3", "unit.c"]
        context = {"source": "src/unit.c", "version": "de", "function": "unit", "address": 0x80001000}
        with patch.object(process.subprocess, "run", return_value=subprocess.CompletedProcess(argv, 3, "stdout detail", "stderr detail")):
            with self.assertRaises(Held) as caught:
                process.run_tool(argv, self.root, "compile", context=context)
        try:
            raise Held("draft", "draft.unit: compiler failed") from caught.exception
        except Held as wrapped:
            result = Result.held("draft", wrapped, "stop: correct the captured compiler diagnostic")
        leaf = result.document()["data"]["fault"]["chain"][-1]["fault"]
        self.assertEqual((leaf["args"], leaf["cwd"], leaf["exit"], leaf["signal"]), (tuple(argv), str(self.root), 3, None))
        self.assertEqual((leaf["stdout"], leaf["stderr"], leaf["context"]), ("stdout detail", "stderr detail", context))

    def test_permission_is_a_native_os_fault_and_signal_has_no_exit_code(self):
        with patch.object(process.subprocess, "run", side_effect=PermissionError(errno.EACCES, "Permission denied")):
            with self.assertRaises(Held) as caught:
                process.run_tool(["cc"], self.root, "compile")
        self.assertEqual((caught.exception.fault["category"], caught.exception.fault["errno"]), ("native-os", errno.EACCES))
        with patch.object(process.subprocess, "run", return_value=subprocess.CompletedProcess(["cc"], -9, "out", "err")):
            with self.assertRaises(Held) as caught:
                process.run_tool(["cc"], self.root, "compile")
        self.assertEqual((caught.exception.fault["exit"], caught.exception.fault["signal"]), (None, 9))


    def test_guidance_finds_native_context_through_an_owning_wrapper(self):
        from unbake.cli.guidance import after
        from types import SimpleNamespace
        cause = Held("compile", "compile.cc: denied", fault={"category": "native-os", "errno": errno.EACCES})
        wrapper = Held("draft", "draft.unit: compiler prerequisite failed")
        wrapper.__cause__ = cause
        self.assertIn("stop: correct the native tool failure", after(SimpleNamespace(command="draft"), wrapper))
