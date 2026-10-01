"""Scratch isolation and tool execution for draft compilation."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

from unbake.project import build
from unbake.project.config import Held, Policy, Project


def scratch_directory(project: Project, scratch: Path, phase: str) -> Path:
    if scratch is None or not str(scratch):
        raise Held(phase, "scratch is required")
    root = Path(project.root).resolve()
    directory = Path(scratch).resolve()
    if directory.is_relative_to(root) and not directory.is_relative_to(project.work.resolve()):
        raise Held(phase, f"paths.work: scratch {directory} is inside project.root {root} outside declared work")
    if directory.is_relative_to(root) and not project.work.resolve().is_relative_to(root):
        raise Held(phase, "paths.work: resolved work path escapes project.root")
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        raise Held(phase, f"scratch {directory}: {error}") from error
    return directory


def read_text(path: Path, phase: str) -> str:
    try:
        return Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        raise Held(phase, f"{path}: {error}") from error


def executable(value: str | Path | None, name: str, phase: str) -> str:
    if value is None or not str(value):
        raise Held(phase, f"policy.{name} is required")
    command = shutil.which(str(value))
    if command is None:
        raise Held(phase, f"policy.{name} executable {value} is absent or not executable")
    return command


def run_tool(argv: list[str], work: Path, phase: str) -> str:
    environment = dict(os.environ, TMPDIR=str(work), TMP=str(work), TEMP=str(work), LC_ALL="C")
    try:
        result = subprocess.run(argv, cwd=work, env=environment, capture_output=True, text=True)
    except OSError as error:
        raise Held(phase, f"{argv[0]}: {error}") from error
    if result.returncode:
        detail = (result.stderr or result.stdout).strip()
        raise Held(phase, f"{argv[0]} exited {result.returncode}: {detail}")
    return result.stdout


def compile_draft(project: Project, policy: Policy, source: Path, version: str, out: Path) -> Path:
    result = Path(build.compile_object(project, policy, source, version, out))
    if result.resolve() != out.resolve() or not out.is_file():
        raise Held("try", f"build.compile_object must write {out}; returned {result}")
    return result
