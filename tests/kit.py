"""Small builders shared by the reduction tests: fake executables and a complete valid host table."""

import copy
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

EXECUTABLES = (
    "make cpp mips_as mips_ld mips_objcopy mips_objdump mips_readelf n64link splat m2c"
).split()


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
    tools.update(
        path=[str(bin_dir)], permuter_archive=str(archive), permuter_sha256="b" * 64
    )
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
