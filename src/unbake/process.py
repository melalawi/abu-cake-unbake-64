"""Native execution with per-child accounting and process-group timeouts."""

import ctypes
import os
import selectors
import signal
import subprocess
from collections.abc import Mapping, Sequence
from contextlib import ExitStack, suppress
from pathlib import Path
from time import perf_counter_ns

from unbake import effort
from unbake.contracts import Config, Finding, NativeResult, Refusal


def _kill(pid: int) -> None:
    with suppress(ProcessLookupError):
        os.killpg(pid, signal.SIGKILL)


def _reap(group: int) -> float:
    """Kills what the child left in its process group and reaps it, returning the CPU seconds of every member: a
    killed child never waited for its own children, which are ours (subreaper) and only we can account for."""
    _kill(group)
    cpu = 0.0
    while True:
        try:
            _, _, usage = os.wait4(-group, 0)
        except ChildProcessError:
            return cpu
        cpu += usage.ru_utime + usage.ru_stime


def scratch(root: Path) -> Path:
    """The temporary directory every child of a project gets (TMPDIR), created when first needed."""
    path = root / ".unbake" / "tmp"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _execute(name, argv, cwd, stdin, stdout_path, timeout, outputs, tmp):
    start = perf_counter_ns()
    ctypes.CDLL(None).prctl(36, 1, 0, 0, 0)  # PR_SET_CHILD_SUBREAPER: orphans of a killed child come to us to be reaped
    deadline = None if timeout is None else start + timeout * 1_000_000_000
    streams = {"stdout": bytearray(), "stderr": bytearray()}
    with ExitStack() as stack:
        destination = subprocess.PIPE
        if stdout_path is not None:
            destination = stack.enter_context(stdout_path.open("wb"))
        proc = subprocess.Popen(
            argv, cwd=cwd, stdin=subprocess.PIPE, stdout=destination,
            stderr=subprocess.PIPE, start_new_session=True, env={**os.environ, "TMPDIR": str(tmp)},
        )
        try:
            selector = stack.enter_context(selectors.DefaultSelector())
            for role, pipe in (("stdout", proc.stdout), ("stderr", proc.stderr)):
                if pipe is not None:
                    stack.enter_context(pipe)
                    os.set_blocking(pipe.fileno(), False)
                    selector.register(pipe, selectors.EVENT_READ, role)
            stack.enter_context(proc.stdin)
            pending = memoryview(stdin)
            if pending:
                os.set_blocking(proc.stdin.fileno(), False)
                selector.register(proc.stdin, selectors.EVENT_WRITE, "stdin")
            else:
                proc.stdin.close()
            while selector.get_map():
                remaining = None
                if deadline is not None:
                    remaining = (deadline - perf_counter_ns()) / 1_000_000_000
                    if remaining <= 0:
                        _kill(proc.pid)
                        deadline = None
                        remaining = None
                for key, _ in selector.select(remaining):
                    pipe = key.fileobj
                    if key.data == "stdin":
                        try:
                            pending = pending[os.write(pipe.fileno(), pending[:65536]):]
                        except BrokenPipeError:
                            pending = pending[:0]
                        except BlockingIOError:
                            continue
                        if not pending:
                            selector.unregister(pipe)
                            pipe.close()
                    else:
                        try:
                            chunk = os.read(pipe.fileno(), 65536)
                        except BlockingIOError:
                            continue
                        if chunk:
                            streams[key.data].extend(chunk)
                        else:
                            selector.unregister(pipe)
                            pipe.close()
            _, status, usage = os.wait4(proc.pid, 0)
            proc.returncode = os.waitstatus_to_exitcode(status)
            orphans = _reap(proc.pid)
        finally:
            if proc.returncode is None:
                _kill(proc.pid)
                _, status, _ = os.wait4(proc.pid, 0)
                proc.returncode = os.waitstatus_to_exitcode(status)
                _reap(proc.pid)
    code = proc.returncode
    result = NativeResult(
        tuple(str(arg) for arg in argv), str(cwd),
        code if code >= 0 else None, -code if code < 0 else None,
        bytes(streams["stdout"]), bytes(streams["stderr"]),
        (perf_counter_ns() - start) / 1_000_000_000,
        usage.ru_utime + usage.ru_stime + orphans, usage.ru_maxrss * 1024, dict(outputs),
    )
    effort.record_native(name, result, start)
    return result


def run(
    name: str, argv: Sequence[str], cwd: Path, *, tmp: Path, stdin: bytes = b"",
    stdout_path: Path | None = None, timeout: float | None = None,
    outputs: Mapping[str, Path] = {},
) -> NativeResult:
    with effort.stage("process.run"):
        executable = str(argv[0]) if argv else ""
        if not executable or not Path(executable).is_file() or not os.access(executable, os.X_OK):
            raise Refusal(Finding(
                "native.missing_tool", reason="the tool is not an executable file", path=executable,
            ))
        if Path(executable).name == "make" and "-j" not in [a[:2] for a in argv[1:]]:
            raise Refusal(Finding("native.exit", reason=f"{Path(executable).name} runs only with -j from host.workers"))
        try:
            tmp.mkdir(parents=True, exist_ok=True)
            return _execute(name, argv, cwd, stdin, stdout_path, timeout, outputs, tmp)
        except OSError as error:
            raise Refusal(Finding("native.exit", reason=str(error))) from error


def tool(config: Config, name: str, *, kind: str | None = None) -> Path:
    try:
        return config.host.tools[name]
    except KeyError:
        raise Refusal(Finding(
            "native.missing_tool", reason=f"host tools.{name} is not configured"
            + (f" for unit kind {kind}" if kind else ""),
        )) from None


def git(config: Config, *args: str) -> NativeResult:
    with effort.stage("process.git"):
        result = run("git " + args[0], [tool(config, "git"), *args], config.project.root,
                     tmp=scratch(config.project.root))
        if result.exit != 0:
            lines = result.stderr.decode("utf-8", errors="replace").splitlines()
            raise Refusal(Finding(
                "native.exit", reason=lines[-1] if lines else "git exited with an error",
                missing=(f"git {' '.join(args)} exit {result.exit}",),
            ))
        return result
