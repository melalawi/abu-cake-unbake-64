"""Run one external tool and name its failure; read text inputs and name their failure."""

from __future__ import annotations

import os
import subprocess
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from unbake.config import Held


def temporary_environment(work: Path, env: Mapping[str, str] | None = None) -> dict[str, str]:
    """Pin child scratch, including SQLite spill files, to the caller's explicit storage."""
    directory = work.resolve()
    directory.mkdir(parents=True, exist_ok=True)
    return dict(
        os.environ if env is None else env,
        TMPDIR=str(directory),
        TMP=str(directory),
        TEMP=str(directory),
        SQLITE_TMPDIR=str(directory),
    )


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
    encoding: str
    errors: str
    context: dict[str, Any]


def run_native(
    argv: list[str],
    work: Path,
    phase: str,
    *,
    context: dict[str, Any] | None = None,
    env: dict[str, str] | None = None,
    temporary_root: Path | None = None,
    stdin: str | None = None,
) -> NativeResult:
    """The one native result/fault boundary, retaining both streams and exact invocation."""
    environment = dict(temporary_environment(work if temporary_root is None else temporary_root, env), LC_ALL="C")
    key = f"{phase}.{Path(argv[0]).name}"
    native_input: dict[str, Any] = {"input": stdin} if stdin is not None else {}
    try:
        completed = subprocess.run(
            argv,
            cwd=work,
            env=environment,
            **native_input,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="surrogateescape",
        )
    except OSError as error:
        result = NativeResult(
            tuple(argv),
            str(work),
            None,
            None,
            "",
            str(error),
            "native-os",
            error.errno,
            "utf-8",
            "surrogateescape",
            context or {},
        )
        raise Held(phase, f"{key}: {Path(argv[0]).name}: {error.strerror}", fault=asdict(result)) from error
    status = completed.returncode
    result = NativeResult(
        tuple(argv),
        str(work),
        status if status >= 0 else None,
        -status if status < 0 else None,
        completed.stdout,
        completed.stderr,
        "success" if status == 0 else "native-signal" if status < 0 else "native-exit",
        None,
        "utf-8",
        "surrogateescape",
        context or {},
    )
    if status:
        detail = next(iter((completed.stderr or completed.stdout).strip().splitlines()), "no diagnostic")
        cause = f"signal {-status}" if status < 0 else f"exit {status}"
        raise Held(phase, f"{key}: {Path(argv[0]).name} failed ({cause}): {detail}", fault=asdict(result))
    return result


def run_tool(
    argv: list[str],
    work: Path,
    phase: str,
    *,
    context: dict[str, Any] | None = None,
    temporary_root: Path | None = None,
) -> str:
    return run_native(argv, work, phase, context=context, temporary_root=temporary_root).stdout


def fault(error: BaseException) -> dict[str, Any]:
    """An owning cause chain, including native results captured at the action boundary."""
    import traceback

    chain = []
    seen: set[int] = set()
    current: BaseException | None = error
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, Held):
            row: dict[str, Any] = {"phase": current.phase, "key": current.key, "reason": current.reason}
            if current.failures:
                row["failures"] = list(current.failures)
            if current.fault is not None:
                row["fault"] = current.fault
        else:
            frames = traceback.extract_tb(current.__traceback__)
            row = {
                "category": "python",
                "type": type(current).__name__,
                "reason": str(current),
                "frames": [
                    {"file": Path(frame.filename).name, "line": frame.lineno, "function": frame.name}
                    for frame in frames
                ],
            }
        chain.append(row)
        current = current.__cause__ or (None if current.__suppress_context__ else current.__context__)
    return {"chain": chain}


def read_text(path: Path, phase: str) -> str:
    try:
        return Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        raise Held(phase, f"{path}: {error}") from error
