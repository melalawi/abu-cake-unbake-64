"""Pinned objdiff symbol scores, cached by object contents."""

from __future__ import annotations

import hashlib
import json
import math
import subprocess
import threading
from pathlib import Path
from typing import cast

from unbake.project.cache import Cache, key
from unbake.project.config import Held, Policy, Project

verified: set[tuple[Path, str]] = set()
_verification_lock = threading.Lock()


def objdiff_cli(policy: Policy, phase: str = "score") -> Path:
    """Verify each configured executable/digest pair once in this process."""
    path = getattr(policy, "objdiff_cli", None)
    if not path:
        raise Held(phase, "policy.objdiff_cli is missing")
    expected = getattr(policy, "objdiff_sha256", None)
    if not isinstance(expected, str) or len(expected) != 64:
        raise Held(phase, "policy.objdiff_sha256 must be a SHA-256 digest")
    try:
        int(expected, 16)
    except ValueError as error:
        raise Held(phase, "policy.objdiff_sha256 must be a SHA-256 digest") from error
    path = Path(path).resolve()
    identity = (path, expected.lower())
    with _verification_lock:
        if identity not in verified:
            try:
                with path.open("rb") as stream:
                    actual = hashlib.file_digest(stream, "sha256").hexdigest()
            except OSError as error:
                raise Held(phase, f"policy.objdiff_cli {path}: {error}") from error
            if actual != expected.lower():
                raise Held(phase, f"policy.objdiff_sha256 for {path}: expected {expected}, found {actual}")
            verified.add(identity)
    return path


def percent(value: object, name: str, phase: str = "score") -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise Held(phase, f"{name} must be a percentage")
    value = float(value)
    if not math.isfinite(value) or not 0 <= value <= 100:
        raise Held(phase, f"{name} must be between 0 and 100")
    return value


def weakest(scores: dict[str, float]) -> float:
    if not isinstance(scores, dict) or not scores:
        raise Held("score", "scores must contain at least one VERSION")
    return min(percent(value, f"scores[{version}]") for version, value in scores.items())


def _symbol_score(document: object, function: str) -> float:
    if not isinstance(document, dict) or not isinstance(document.get("left"), dict):
        raise Held("score", "objdiff JSON left is missing")
    symbols = document["left"].get("symbols")
    if not isinstance(symbols, list):
        raise Held("score", "objdiff JSON left.symbols is missing")
    symbols = [
        symbol
        for symbol in symbols
        if isinstance(symbol, dict) and symbol.get("name") == function and symbol.get("kind") == "SYMBOL_FUNCTION"
    ]
    if len(symbols) != 1:
        raise Held("score", f"objdiff JSON left.symbols function {function}: expected one symbol")
    # Proto3 JSON omits scalar zeroes, including a function's zero match_percent.
    return percent(symbols[0].get("match_percent", 0.0), f"{function}.match_percent")


def fuzzy(project: Project, policy: Policy, v: str, function: str, target_obj: Path, base_obj: Path) -> float:
    project.version(v)
    if not isinstance(function, str) or not function:
        raise Held("score", "function is missing")
    tool = objdiff_cli(policy)
    cache_root = getattr(policy, "cache_root", None)
    if not cache_root:
        raise Held("score", "policy.cache_root is missing")
    for name, path in (("target_obj", target_obj), ("base_obj", base_obj)):
        if path is None or not Path(path).is_file():
            raise Held("score", f"{name} {path} is missing")
    target_obj, base_obj = Path(target_obj).resolve(), Path(base_obj).resolve()
    try:
        identity = key("objdiff-symbol-json", policy.objdiff_sha256.lower(), v, function, target_obj, base_obj)

        def generate(output: Path) -> None:
            try:
                result = subprocess.run(
                    [
                        str(tool),
                        "diff",
                        "-1",
                        str(target_obj),
                        "-2",
                        str(base_obj),
                        function,
                        "--format",
                        "json",
                        "--output",
                        str(output),
                    ],
                    capture_output=True,
                    text=True,
                    check=False,
                )
            except OSError as error:
                raise Held("score", f"policy.objdiff_cli {tool}: {error}") from error
            if result.returncode:
                raise Held("score", f"objdiff diff {function} VERSION {v}: {result.stderr.strip()}")
            _read_score(output, function)

        output = Cache(Path(cache_root)).produce("score", identity, generate)
        return _read_score(output, function)
    except OSError as error:
        raise Held("score", f"score cache {cache_root}: {error}") from error


def _read_score(path: Path, function: str) -> float:
    try:
        document = json.loads(Path(path).read_bytes())
    except (OSError, ValueError) as error:
        raise Held("score", f"objdiff JSON {path}: {error}") from error
    return _symbol_score(document, function)


def diff(policy: Policy, version: str, function: str, target: Path, candidate: Path, output: Path) -> dict[str, object]:
    """Retain instruction rows from a real relocatable-object comparison."""
    tool = objdiff_cli(policy, "try")
    result = subprocess.run(
        [
            str(tool),
            "diff",
            "-1",
            str(target),
            "-2",
            str(candidate),
            function,
            "--format",
            "json",
            "--output",
            str(output),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode:
        raise Held("try", f"objdiff {function} VERSION {version}: {result.stderr.strip()}")
    return cast(dict[str, object], json.loads(output.read_bytes()))
