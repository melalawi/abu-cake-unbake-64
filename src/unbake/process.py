"""Run one external tool and name its failure; read text inputs and name their failure."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from dataclasses import dataclass, asdict
from typing import Any

from unbake.config import Held


@dataclass(frozen=True)
class NativeResult:
    args: tuple[str, ...]
    cwd: str
    exit: int | None
    signal: int | None
    stdout: str
    stderr: str
    category: str
    errno: int | None
    context: dict[str, Any]


def run_native(argv: list[str], work: Path, phase: str, *, context: dict[str, Any] | None = None) -> NativeResult:
    """The one native result/fault boundary, retaining both streams and exact invocation."""
    environment = dict(os.environ, TMPDIR=str(work), TMP=str(work), TEMP=str(work), LC_ALL="C")
    key = f"{phase}.{Path(argv[0]).name}"
    try:
        completed = subprocess.run(argv, cwd=work, env=environment, capture_output=True, text=True, encoding="utf-8", errors="surrogateescape")
    except OSError as error:
        result = NativeResult(tuple(argv), str(work), None, None, "", str(error), "native-os", error.errno, context or {})
        raise Held(phase, f"{key}: {Path(argv[0]).name}: {error.strerror}", fault=asdict(result)) from error
    status = completed.returncode
    result = NativeResult(tuple(argv), str(work), status if status >= 0 else None,
                          -status if status < 0 else None, completed.stdout, completed.stderr,
                          "success" if status == 0 else "native-signal" if status < 0 else "native-exit", None, context or {})
    if status:
        detail = next(iter((completed.stderr or completed.stdout).strip().splitlines()), "no diagnostic")
        cause = f"signal {-status}" if status < 0 else f"exit {status}"
        raise Held(phase, f"{key}: {Path(argv[0]).name} failed ({cause}): {detail}", fault=asdict(result))
    return result


def run_tool(argv: list[str], work: Path, phase: str, *, context: dict[str, Any] | None = None) -> str:
    return run_native(argv, work, phase, context=context).stdout


def fault(error: BaseException) -> dict[str, Any]:
    """An owning cause chain, including native results captured at the action boundary."""
    import traceback

    chain = []
    seen: set[int] = set()
    current: BaseException | None = error
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, Held):
            row = {"phase": current.phase, "key": current.key, "reason": current.reason}
            if current.fault is not None:
                row["fault"] = current.fault
        else:
            frames = traceback.extract_tb(current.__traceback__)
            row = {"category": "python", "type": type(current).__name__, "reason": str(current),
                   "frames": [{"file": Path(frame.filename).name, "line": frame.lineno, "function": frame.name}
                              for frame in frames]}
        chain.append(row)
        current = current.__cause__ or (None if current.__suppress_context__ else current.__context__)
    return {"chain": chain}


def read_text(path: Path, phase: str) -> str:
    try:
        return Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        raise Held(phase, f"{path}: {error}") from error
