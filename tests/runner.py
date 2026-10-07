"""Run pure unit tests in isolated workers, with process creation forbidden."""

import io
import json
import multiprocessing.process
import multiprocessing.util
import os
import signal
import struct
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

_ACTIVE = True
_EVENTS = {"subprocess.Popen", "os.system", "os.fork", "os.forkpty", "os.posix_spawn", "os.exec", "os.spawn"}


def forbidden(*args, **kwargs):
    raise AssertionError("unit tests must mock the external tool boundary; subprocess creation is forbidden")


def audit(event, args):
    if _ACTIVE and event in _EVENTS:
        forbidden(event, args)


def cases(suite):
    for test in suite:
        if isinstance(test, unittest.TestSuite):
            yield from cases(test)
        else:
            yield test


class Jobs(unittest.TestSuite):
    """Share a pipe of test indices so a slow case cannot strand a worker."""

    _cleanup = False

    def __init__(self, tests, reader):
        super().__init__()
        self.tests = tests
        self.reader = reader

    def __iter__(self):
        while data := os.read(self.reader, 4):
            index = struct.unpack("!I", data)[0]
            if index == 0xFFFFFFFF:
                return
            yield self.tests[index]


class Runner:
    def __init__(self, stream=None, verbosity=1, failfast=False, buffer=False, **kwargs):
        self.stream = stream or sys.stderr
        self.verbosity = verbosity
        self.failfast = failfast
        self.buffer = buffer

    def run(self, suite):
        global _ACTIVE
        tests = list(cases(suite))
        if not tests:
            raise AssertionError("test selection is empty")
        workers = min(24, 2 * (os.cpu_count() or 1), len(tests))
        if workers <= 1 or os.environ.get("UNIT_TEST_WORKERS") == "1":
            return unittest.TextTestRunner(
                stream=self.stream, verbosity=self.verbosity, failfast=self.failfast, buffer=self.buffer
            ).run(unittest.TestSuite(tests))
        started = time.perf_counter()
        result = unittest.TestResult()
        children = []
        reader, writer = os.pipe()
        with tempfile.TemporaryDirectory(prefix="unit-results-") as temporary:
            try:
                # Fork only here, before any test runs; each worker then forbids all process creation.
                _ACTIVE = False
                for index in range(workers):
                    pid = os.fork()
                    if pid == 0:
                        _ACTIVE = True
                        os.close(writer)
                        output = io.StringIO()
                        with redirect_stdout(output), redirect_stderr(output):
                            measured = unittest.TextTestRunner(
                                stream=output, verbosity=self.verbosity, failfast=self.failfast, buffer=self.buffer
                            ).run(Jobs(tests, reader))
                        document = dict(
                            count=measured.testsRun,
                            failures=[(t.id(), detail) for t, detail in measured.failures],
                            errors=[(t.id(), detail) for t, detail in measured.errors],
                            skipped=[(t.id(), reason) for t, reason in measured.skipped],
                            expectedFailures=[(t.id(), detail) for t, detail in measured.expectedFailures],
                            unexpectedSuccesses=[t.id() for t in measured.unexpectedSuccesses],
                            output=output.getvalue() if self.verbosity > 1 else "",
                        )
                        Path(temporary, str(index)).write_text(json.dumps(document))
                        os._exit(0)
                    children.append((pid, index))
                _ACTIVE = True
                os.close(reader)
                reader = -1
                jobs = b"".join(struct.pack("!I", i) for i in range(len(tests))) + b"\xff" * (workers * 4)
                while jobs:
                    jobs = jobs[os.write(writer, jobs) :]
                os.close(writer)
                writer = -1
                for pid, index in children:
                    _, status = os.waitpid(pid, 0)
                    path = Path(temporary, str(index))
                    if status or not path.is_file():
                        result.errors.append((f"worker {index}", f"worker exited with status {status}"))
                        continue
                    document = json.loads(path.read_text())
                    result.testsRun += document["count"]
                    for name in ("failures", "errors", "skipped", "expectedFailures", "unexpectedSuccesses"):
                        getattr(result, name).extend(document[name])
                    if document["output"]:
                        self.stream.write(document["output"])
            finally:
                _ACTIVE = True
                for descriptor in (reader, writer):
                    if descriptor != -1:
                        os.close(descriptor)
                for pid, _ in children:
                    try:
                        pending, _ = os.waitpid(pid, os.WNOHANG)
                        if pending == 0:
                            os.kill(pid, signal.SIGTERM)
                            os.waitpid(pid, 0)
                    except ChildProcessError:
                        pass
        for name in ("errors", "failures"):
            for test, detail in getattr(result, name):
                self.stream.write(f"\n{name[:-1].upper()}: {test}\n{detail}\n")
        self.stream.write(
            f"\nRan {result.testsRun} tests in {time.perf_counter() - started:.3f}s ({workers} workers)\n\n"
        )
        if result.wasSuccessful():
            self.stream.write("OK\n")
        else:
            self.stream.write(f"FAILED (failures={len(result.failures)}, errors={len(result.errors)})\n")
        return result


def main():
    # Keep fixture roots canonical when the caller supplies a symlinked TMPDIR.
    tempfile.tempdir = str(Path(tempfile.gettempdir()).resolve())

    sys.addaudithook(audit)
    with (
        patch.object(subprocess, "Popen", side_effect=forbidden),
        patch.object(multiprocessing.process.BaseProcess, "start", side_effect=forbidden),
        patch.object(multiprocessing.util, "spawnv_passfds", side_effect=forbidden),
        patch.object(os, "fsync"),
    ):
        # Unit fixtures verify writes and publication; disk durability is an OS service.
        unittest.main(module=None, defaultTest="discover", testRunner=Runner, verbosity=2)


if __name__ == "__main__":
    main()
