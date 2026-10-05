"""Small builders shared by the reduction tests: fake executables and a complete valid host table."""

import copy
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

# Generous step budgets: a test that checks budgets sets its own.
BUDGETS = {
    "recompute_seconds": 3600,
    "unchanged_seconds": 3600,
    "changed_seconds": 3600,
    "facts_miss_fraction": 1.0,
    "main_rss_bytes": 1 << 40,
    "worker_rss_bytes": 1 << 40,
}
# A host for code that only reads the budgets (steps.ensure in mocked step tables).
BUDGET_HOST = SimpleNamespace(**BUDGETS)

EXECUTABLES = [
    "make",
    "cpp",
    "mips_as",
    "mips_ld",
    "mips_objcopy",
    "mips_objdump",
    "mips_readelf",
    "n64link",
    "splat",
    "m2c",
]


def executable(directory: Path, name: str, text: str = "#!/bin/sh\nexit 0\n") -> Path:
    path = directory / name
    path.write_text(text)
    path.chmod(0o755)
    return path


def host_values(directory: Path) -> dict[str, dict[str, object]]:
    """Every unbake.toml key with a valid value; tests break one key at a time."""
    bin_dir = directory / "bin"
    bin_dir.mkdir(exist_ok=True)
    tools: dict[str, object] = {name: str(executable(bin_dir, name)) for name in EXECUTABLES}
    archive = directory / "permuter.tar"
    archive.write_bytes(b"archive")
    tools.update(path=[str(bin_dir)], permuter_archive=str(archive), permuter_sha256="b" * 64)
    return {
        "resources": {
            "cores": 4,
            "workers": 2,
            "memory_total_bytes": 8_000,
            "memory_parent_bytes": 1_000,
            "memory_worker_bytes": 2_000,
        },
        "cache": {"root": str(directory / "cache"), "max_bytes": 1_000, "trim_to_bytes": 500, "memory_bytes": 100},
        "tools": tools,
        "setup": {
            "version_jobs": 2,
            "probe_count": 3,
            "same_game_similarity": 0.5,
            "symbol_similarity_threshold": 0.9,
            "symbol_similarity_margin": 0.1,
        },
        "search": {"stall_trials": 100, "beam": 4},
        "cycle": {"min_bytes": 16, "max_bytes": 4096, "min_history": 3, "debounce_ms": 200},
        "budgets": dict(BUDGETS),
        "publish": {
            "remote": "origin",
            "branch": "main",
            "author_name": "Mo",
            "author_email": "mo@example.com",
            "credential_env": "UNBAKE_TOKEN",
        },
    }


def edited(values: dict, dotted: str, value: object = ...) -> dict:
    """A deep copy of values with section.key replaced, or removed when value is omitted."""
    result = copy.deepcopy(values)
    section, key = dotted.split(".", 1)
    if value is ...:
        del result[section][key]
    else:
        result[section][key] = value
    return result


class TempCase(unittest.TestCase):
    """A test case with a fresh resolved temporary directory in self.root."""

    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()


def boundary(module, execute):
    """Patch MODULE's subprocess.run with EXECUTE (unit tests never start processes)."""
    return patch.object(module, "subprocess", SimpleNamespace(**{**vars(subprocess), "run": execute}))


def git_init(command, *, cwd, **kwargs):
    assert command[1:] == ["init", "-b", "main"], command
    (Path(cwd) / ".git").mkdir()
    return subprocess.CompletedProcess(command, 0, "", "")


def with_value(host, dotted: str, value: object):
    """A copy of HOST with one unbake.toml value replaced (Host is immutable)."""
    from unbake.config import Host

    section, key = dotted.split(".", 1)
    values = copy.deepcopy(host.values)
    values.setdefault(section, {})[key] = str(value) if isinstance(value, Path) else value
    return Host(values, host.command, host.sources)


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
