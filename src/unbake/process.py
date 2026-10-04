"""Run one external tool and name its failure; read text inputs and name their failure."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from unbake.config import Held


def run_tool(argv: list[str], work: Path, phase: str) -> str:
    """Run argv in work with a private TMPDIR and the C locale; return stdout or refuse with stderr."""
    environment = dict(os.environ, TMPDIR=str(work), TMP=str(work), TEMP=str(work), LC_ALL="C")
    try:
        result = subprocess.run(argv, cwd=work, env=environment, capture_output=True, text=True)
    except OSError as error:
        raise Held(phase, f"{argv[0]}: {error}") from error
    if result.returncode:
        detail = (result.stderr or result.stdout).strip()
        raise Held(phase, f"{argv[0]} exited {result.returncode}: {detail}")
    return result.stdout


def read_text(path: Path, phase: str) -> str:
    try:
        return Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        raise Held(phase, f"{path}: {error}") from error
