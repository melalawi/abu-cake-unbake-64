"""Explicit file-operation and Git boundary fakes for small unit fixtures."""

import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


def boundary(module, execute):
    return patch.object(module, "subprocess", SimpleNamespace(**{**vars(subprocess), "run": execute}))


def copy(command, **kwargs):
    assert command[:3] == ["cp", "-a", "--reflink=auto"], command
    destination = Path(command[-1])
    for item in command[3:-1]:
        source = Path(item)
        if source.is_dir():
            shutil.copytree(source, destination / source.name, symlinks=True)
        else:
            shutil.copy2(source, destination / source.name, follow_symlinks=False)
    return subprocess.CompletedProcess(command, 0, "", "")


def git_init(command, *, cwd, **kwargs):
    assert command[1:] == ["init", "-b", "main"], command
    (Path(cwd) / ".git").mkdir()
    return subprocess.CompletedProcess(command, 0, "", "")


def cli(arguments, *, cwd=None):
    """Call the actual public parser/dispatcher in the current process."""
    import io
    import os
    from contextlib import redirect_stderr, redirect_stdout

    from unbake.cli.main import main

    output, error = io.StringIO(), io.StringIO()
    original = Path.cwd()
    try:
        if cwd is not None:
            os.chdir(cwd)
        with redirect_stdout(output), redirect_stderr(error):
            try:
                status = main(list(map(str, arguments)))
            except SystemExit as exit_status:
                status = exit_status.code
        return subprocess.CompletedProcess(arguments, status, output.getvalue(), error.getvalue())
    finally:
        os.chdir(original)


def script_output(command, **kwargs):
    """Execute a test-authored output script in process, never a host program."""
    import io
    import os
    import re
    import shlex
    import sys
    from contextlib import redirect_stderr, redirect_stdout

    path = Path(command[0])
    source = path.read_text()
    assert source.startswith("#!"), path
    assert "subprocess" not in source and "execv" not in source, path
    if source.startswith("#!/bin/sh"):
        match = re.search(r"printf\s+([^\n]+)", source)
        output = shlex.split(match[1])[0].replace(r"\n", "\n") if match else ""
        status = re.search(r"exit\s+(\d+)", source)
        return subprocess.CompletedProcess(command, int(status[1]) if status else 0, output, "")
    assert source.splitlines()[0] == "#!" + sys.executable, path
    output, error = io.StringIO(), io.StringIO()
    directory = Path.cwd()
    status = 0
    try:
        os.chdir(kwargs.get("cwd", directory))
        with (
            patch.object(sys, "argv", command),
            patch.dict(os.environ, kwargs.get("env", {})),
            redirect_stdout(output),
            redirect_stderr(error),
        ):
            try:
                exec(compile(source, str(path), "exec"), {"__name__": "__main__", "__file__": str(path)})
            except SystemExit as result:
                status = result.code or 0
    finally:
        os.chdir(directory)
    return subprocess.CompletedProcess(command, status, output.getvalue(), error.getvalue())


def cli_process(command, **kwargs):
    import os

    arguments = list(map(str, command))
    if "-m" in arguments:
        marker = arguments.index("-m")
        assert arguments[marker + 1] == "unbake", arguments
        arguments = arguments[marker + 2 :]
    else:
        arguments = (
            arguments[2:]
            if arguments[0].endswith("python")
            or arguments[0].endswith("python3")
            or arguments[0].endswith("python3.13")
            or (len(arguments) > 1 and arguments[1].endswith(".py"))
            else arguments[1:]
        )
    with patch.dict(os.environ, kwargs.get("env", {})):
        return cli(arguments, cwd=kwargs.get("cwd"))


def compile_helper(command, **kwargs):
    """Run our compiler helper in process with its tool calls mocked separately."""
    import io
    import os
    import sys
    from contextlib import redirect_stderr, redirect_stdout

    from unbake.project_tools import compile as compiler

    assert Path(command[1]).name == "compile.py", command
    output, error = io.StringIO(), io.StringIO()
    directory = Path.cwd()
    status = 0
    try:
        os.chdir(kwargs.get("cwd", directory))
        with patch.object(sys, "argv", command[1:]), redirect_stdout(output), redirect_stderr(error):
            try:
                compiler.main()
            except SystemExit as result:
                status = result.code or 0
    finally:
        os.chdir(directory)
    text = kwargs.get("text", False)
    return subprocess.CompletedProcess(
        command,
        status,
        output.getvalue() if text else output.getvalue().encode(),
        error.getvalue() if text else error.getvalue().encode(),
    )


def fixture_tool(command, **kwargs):
    """Execute only fixture-authored compiler output at the helper's boundary."""
    from tests.preprocessor import output

    result = output(command, **kwargs) if "cpp" in Path(command[0]).name else script_output(command, **kwargs)
    if kwargs.get("stderr") == subprocess.STDOUT:
        result.stdout += result.stderr
        result.stderr = ""
    if not kwargs.get("text", False):
        result.stdout = result.stdout.encode()
        result.stderr = result.stderr.encode()
    return result


class Pool:
    """Run the executor boundary in process, preserving ordering and receipts."""

    def __init__(self, *, max_workers, mp_context):
        assert max_workers > 0
        assert mp_context.get_start_method() == "fork"

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def map(self, function, items, *, chunksize):
        assert chunksize > 0
        return map(function, items)

    def submit(self, function, item):
        from concurrent.futures import Future

        future = Future()
        try:
            future.set_result(function(item))
        except BaseException as error:
            future.set_exception(error)
        return future

    def shutdown(self, *, cancel_futures):
        assert cancel_futures


def compiler_registry(case):
    """Supply immutable compiler descriptions to project fixtures."""
    from unbake.project import toolchain

    descriptions = toolchain.registry()
    mock = patch.object(toolchain, "registry", return_value=descriptions)
    mock.start()
    case.addCleanup(mock.stop)
