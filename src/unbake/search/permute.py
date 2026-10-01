"""Bounded external permutations compiled with the project's own recipe."""

from __future__ import annotations

import hashlib
import importlib.util
import math
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tarfile
import tempfile
import time
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

from unbake.decomp.trial import Trial
from unbake.project import makefile, toolchain
from unbake.project.config import Held, Policy, Project
from unbake.search.core import Context, Mutation


def _required(owner: object, name: str, prefix: str) -> Any:
    value = getattr(owner, name, None)
    if value is None or value == "":
        raise Held("permute", f"{prefix}.{name} is required")
    return value


def _outside(project: Project, path: Path, name: str) -> Path:
    path = Path(path).resolve()
    if path.is_relative_to(Path(project.root).resolve()):
        raise Held("permute", f"{name} {path} is inside project.root")
    return path


def checkout(archive: Path, digest: str, work: Path) -> Path:
    if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise Held("permute", "policy.permuter_sha256 must be a lowercase SHA-256")
    try:
        with Path(archive).open("rb") as stream:
            actual = hashlib.file_digest(stream, "sha256").hexdigest()
        if actual != digest:
            raise Held("permute", f"policy.permuter_archive policy.permuter_sha256 expected {digest}, found {actual}")
        checkout = work / "dependency"
        checkout.mkdir()
        with tarfile.open(archive) as package:
            for member in package.getmembers():
                destination = (checkout / member.name).resolve()
                if not destination.is_relative_to(checkout) or not (member.isdir() or member.isfile()):
                    raise Held("permute", f"policy.permuter_archive unsafe member {member.name}")
            package.extractall(checkout, filter="data")
        # The entry point sits at the archive root; src/permuter.py is the library module it imports.
        entries = list(checkout.glob("*/permuter.py"))
        if len(entries) != 1:
            raise Held("permute", "policy.permuter_archive must contain exactly one top-level permuter.py")
        return entries[0]
    except (OSError, tarfile.TarError) as error:
        raise Held("permute", f"policy.permuter_archive: {error}") from error


def compile_script(project: Project, policy: Policy, source_path: Path, version: str, work: Path) -> str:
    """Render a scorer using generated compile.py, preserving unit and VERSION flags."""
    project.version(version)
    cache = _outside(project, _required(policy, "cache_root", "policy"), "policy.cache_root")
    unit = Path(source_path).resolve()
    unit = (
        unit.relative_to(project.root)
        if unit.is_relative_to(project.src)
        else project.src.relative_to(project.root) / (unit.stem + ".c")
    )
    command = [
        sys.executable,
        str(work / "recipe" / "compile.py"),
        "--kind",
        "cc",
        "--recipe",
        str(work / "recipe" / "build.json"),
        "--version",
        version,
        "--unit",
        str(unit),
        "--cache-root",
        str(cache),
        "--non-matching",
        "0",
    ]
    return (
        "#!/bin/sh\nset -eu\n"
        '[ "$#" -eq 3 ] && [ "$2" = "-o" ] || { echo "source -o output required" >&2; exit 2; }\n'
        'source=$(realpath -- "$1")\noutput=$(realpath -m -- "$3")\n'
        f'case "$output" in {shlex.quote(str(work))}/*) ;; *) '
        "echo 'output outside search directory' >&2; exit 2;; esac\n"
        f'cd {shlex.quote(str(project.root))}\nexec {shlex.join(command)} --source "$source" --output "$output"\n'
    )


@dataclass(frozen=True)
class _RunResult:
    ran: bool
    # None means skipped or stopped at the deadline, rather than an early exit.
    returncode: int | None


def _run(command: Sequence[str], cwd: Path, environment: Mapping[str, str], budget: float, log: Path) -> _RunResult:
    try:
        with log.open("wb") as output, log.with_suffix(log.suffix + ".stderr").open("wb") as errors:
            if budget <= 0:
                return _RunResult(False, None)
            process = subprocess.Popen(
                command, cwd=cwd, env=environment, stdout=output, stderr=errors, start_new_session=True
            )
            try:
                status = process.wait(timeout=budget)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
                return _RunResult(True, None)
            except BaseException:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
                raise
        return _RunResult(True, status)
    except OSError as error:
        raise Held("permute", f"permuter {shlex.join(command)}: {error}") from error


def outputs(work: Path) -> Iterator[tuple[int, str]]:
    for directory in sorted(work.glob("output-*")):
        if not re.fullmatch(r"output-\d+-\d+", directory.name):
            raise Held("permute", f"result directory {directory.name}: expected output-SCORE-INDEX")
        if directory.is_symlink():
            raise Held("permute", f"result directory {directory}: symbolic link refused")
        try:
            score_path, source_path = directory / "score.txt", directory / "source.c"
            for path in (score_path, source_path):
                if path.is_symlink():
                    raise Held("permute", f"result {path}: symbolic link refused")
            score = score_path.read_text().split()[0]
            if not re.fullmatch(r"\d+", score) or int(score) != int(directory.name.split("-")[1]):
                raise Held("permute", f"result {score_path}: score disagrees with directory")
            yield int(score), source_path.read_text(encoding="utf-8")
        except (OSError, UnicodeError, IndexError) as error:
            raise Held("permute", f"result {directory}: source.c and score.txt required: {error}") from error


@dataclass(frozen=True)
class Permuter:
    version: str
    target_object: Path
    budget_seconds: float
    ran: bool = field(default=False, init=False)

    def propose(self, source: str, trial: Trial, ctx: Context) -> Iterator[Mutation]:
        """Yield external improvements for the common search loop to confirm."""

        project = cast(Project, _required(ctx, "project", "context"))
        policy = cast(Policy, _required(ctx, "policy", "context"))
        out = _outside(project, _required(ctx, "out", "context"), "context.out")
        source_path = Path(_required(ctx, "source", "context"))
        version = _required(self, "version", "permuter")
        project.version(version)
        target = Path(_required(self, "target_object", "permuter"))
        budget = _required(self, "budget_seconds", "permuter")
        if type(budget) not in (int, float) or not math.isfinite(budget) or budget <= 0:
            raise Held("permute", "permuter.budget_seconds must be positive and finite")
        remaining_deadline = _required(ctx, "deadline", "context")
        if type(remaining_deadline) not in (int, float) or not math.isfinite(remaining_deadline):
            raise Held("permute", "context.deadline must be finite monotonic time")
        deadline = min(time.monotonic() + budget, remaining_deadline)
        if time.monotonic() >= deadline:
            return
        if not isinstance(source, str) or not source.strip():
            raise Held("permute", "source is required")
        function = _required(trial, "function", "trial")
        if not re.fullmatch(r"[A-Za-z_]\w*", function) or source_path.stem != function or source_path.suffix != ".c":
            raise Held("permute", "context.source must be named trial.function.c")
        if not _required(trial, "compares", "trial"):
            raise Held("permute", "trial.compares must name holding VERSIONs")
        archive = _required(policy, "permuter_archive", "policy")
        digest = _required(policy, "permuter_sha256", "policy")
        cores = _required(policy, "cores", "policy")
        if type(cores) is not int or cores <= 0:
            raise Held("permute", "policy.cores must be a positive integer")
        for dependency in ("pycparser", "toml"):
            if importlib.util.find_spec(dependency) is None:
                raise Held("permute", f"dependency {dependency}: missing from interpreter {sys.executable}")
        compiler = project.compiler_for(source_path)
        family = toolchain.specification(compiler.id).family
        if family not in ("gcc", "ido"):
            raise Held("permute", f"compiler.family {family}: unsupported scorer")
        toolchain.verify(project.tools / compiler.id, toolchain.specification(compiler.id))
        try:
            if not target.is_file():
                raise Held("permute", f"permuter.target_object {target}: missing object")
            out.mkdir(parents=True, exist_ok=True)
            work = Path(tempfile.mkdtemp(prefix="permute-", dir=out))
            entry = checkout(archive, digest, work)
            recipe = work / "recipe"
            recipe.mkdir()
            for name, content in makefile.helpers(project).items():
                (recipe / Path(name).name).write_text(content, encoding="utf-8")
            shutil.copyfile(project.tools / "compiler.sha256", recipe / "compiler.sha256")
            (work / "base.c").write_text(source, encoding="utf-8")
            environment = dict(
                os.environ, TMPDIR=str(work), TMP=str(work), TEMP=str(work), PYTHONDONTWRITEBYTECODE="1", LC_ALL="C"
            )
            shutil.copyfile(target, work / "target.o")
            (work / "settings.toml").write_text(f'func_name = "{function}"\ncompiler_type = "{family}"\n')
            script = work / "compile.sh"
            script.write_text(compile_script(project, policy, source_path, version, work))
            script.chmod(0o755)
            command = [
                sys.executable,
                "-u",
                str(entry),
                str(work),
                "-j",
                str(cores),
                "--stack-diffs",
                # The pinned scorer ignores branch targets by default. Its strict
                # branch mode cannot decode R_MIPS_PC16 relocations in assembly objects.
                "--quiet",
            ]
            # Leave time to evaluate emitted candidates in the common search loop.
            log_path = work / "permuter.log"
            result = _run(command, work, environment, max(0, deadline - time.monotonic()) / 2, log_path)
            if not result.ran:
                return
            object.__setattr__(self, "ran", True)
            if result.returncode not in (None, 0):
                stderr_path = log_path.with_suffix(log_path.suffix + ".stderr")
                stderr = stderr_path.read_text(encoding="utf-8", errors="replace").strip()
                raise Held("permute", f"permuter {entry} exited {result.returncode}: {stderr}; inspect {stderr_path}")
            log = log_path.read_text(encoding="utf-8")
            baseline = re.findall(r"base score = (\d+)", log)
            if result.returncode is None and not baseline:
                return
            if len(baseline) != 1:
                raise Held("permute", "permuter.log base score is required")
            seen = {hashlib.sha256(source.encode()).hexdigest()}
            for score, candidate in outputs(work):
                if score >= int(baseline[0]):
                    continue
                digest = hashlib.sha256(candidate.encode()).hexdigest()
                if digest in seen:
                    continue
                seen.add(digest)
                yield Mutation("permute", f"external score {score}", candidate)
        except (OSError, UnicodeError, ValueError) as error:
            raise Held("permute", f"search output: {error}") from error
