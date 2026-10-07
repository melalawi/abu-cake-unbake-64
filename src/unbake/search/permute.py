"""Bounded external permutations compiled with the project's own recipe."""

from __future__ import annotations

import hashlib
import importlib.util
import math
import os
import re
import shlex
import subprocess
import sys
import tarfile
import tempfile
import time
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

from unbake import atomic as atomic_files
from unbake import pool
from unbake.compilers import registry as toolchain
from unbake.config import Held, Host, Project
from unbake.process import capture, temporary_environment
from unbake.process import named as cause_named
from unbake.search.core import Context, Mutation
from unbake.work.compare import Compared


def _required(owner: object, name: str, prefix: str) -> Any:
    value = getattr(owner, name, None)
    if value is None or value == "":
        raise Held(
            cause_named(
                "search.permute._required", f"{prefix}.{name} is required", owner="search.permute", stage="permute"
            )
        )
    return value


def _outside(project: Project, path: Path, name: str) -> Path:
    path = Path(path).resolve()
    if path.is_relative_to(Path(project.root).resolve()):
        raise Held(
            cause_named(
                "search.permute._outside",
                f"{name} {path} is inside project.root",
                owner="search.permute",
                stage="permute",
            )
        )
    return path


def checkout(archive: Path, digest: str, work: Path) -> Path:
    if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise Held(
            cause_named(
                "search.permute.checkout",
                "policy.permuter_sha256 must be a lowercase SHA-256",
                owner="search.permute",
                stage="permute",
            )
        )
    try:
        with Path(archive).open("rb") as stream:
            actual = hashlib.file_digest(stream, "sha256").hexdigest()
        if actual != digest:
            raise Held(
                cause_named(
                    "search.permute.checkout",
                    f"policy.permuter_archive policy.permuter_sha256 expected {digest}, found {actual}",
                    owner="search.permute",
                    stage="permute",
                )
            )
        checkout = work / "dependency"
        checkout.mkdir()
        with tarfile.open(archive) as package:
            for member in package.getmembers():
                destination = (checkout / member.name).resolve()
                if not destination.is_relative_to(checkout) or not (member.isdir() or member.isfile()):
                    raise Held(
                        cause_named(
                            "search.permute.checkout",
                            f"policy.permuter_archive unsafe member {member.name}",
                            owner="search.permute",
                            stage="permute",
                        )
                    )
            package.extractall(checkout, filter="data")
        # The entry point sits at the archive root; src/permuter.py is the library module it imports.
        entries = list(checkout.glob("*/permuter.py"))
        if len(entries) != 1:
            raise Held(
                cause_named(
                    "search.permute.checkout",
                    "policy.permuter_archive must contain exactly one top-level permuter.py",
                    owner="search.permute",
                    stage="permute",
                )
            )
        return entries[0]
    except (OSError, tarfile.TarError) as error:
        raise Held(
            capture(
                error,
                cause=cause_named(
                    "policy.permuter_archive",
                    f"policy.permuter_archive: {error}",
                    owner="search.permute",
                    stage="permute",
                ),
            )
        ) from error


def compile_script(project: Project, policy: Host, source_path: Path, version: str, work: Path) -> str:
    """A permuter scorer script running the unit's exact build steps (compilers.drivers) on a candidate source."""
    from unbake.compilers import drivers

    project.version(version)
    unit = Path(source_path).stem
    tools = drivers.Tools(str(policy.cpp), str(policy.mips_as), str(policy.n64link))
    steps = drivers.steps(project, version, unit, '"$source"', tools)
    cc = str(project.compiler_for(unit).cc)
    compile_ = (cc, *steps.compile[1:])

    def words(argv: tuple[str, ...]) -> str:
        return " ".join(item if item == '"$source"' else shlex.quote(item) for item in argv)

    lines = [
        "#!/bin/sh",
        "set -eu",
        '[ "$#" -eq 3 ] && [ "$2" = "-o" ] || { echo "source -o output required" >&2; exit 2; }',
        'source=$(realpath -- "$1")',
        'output=$(realpath -m -- "$3")',
        f'case "$output" in {shlex.quote(str(work))}/*) ;; '
        '*) echo "output outside search directory" >&2; exit 2;; esac',
        f"scratch=$(mktemp -d {shlex.quote(str(work))}/build.XXXXXX)",
        f"cd {shlex.quote(str(project.root))}",
        f'{words(steps.preprocess)} > "$scratch/{unit}.i"',
        'cd "$scratch"',
        words(compile_),
    ]
    if steps.assemble is not None:
        lines.append(words(steps.assemble))
    lines.append(f'mv -- "$scratch/{unit}.o" "$output"')
    lines.append('rm -rf -- "$scratch"')
    return "\n".join(lines) + "\n"


# Runs outside the permuter's group: when the owner (argv 1) exits, even by SIGKILL, the group (argv 2) dies.
_WATCH = (
    "import os, select, signal, sys\n"
    "select.select([os.pidfd_open(int(sys.argv[1]))], [], [])\n"
    "try:\n"
    "    os.killpg(int(sys.argv[2]), signal.SIGKILL)\n"
    "except ProcessLookupError:\n"
    "    pass\n"
)


@dataclass(frozen=True)
class _RunResult:
    ran: bool
    # None means skipped or stopped at the deadline, rather than an early exit.
    returncode: int | None


def _run(command: Sequence[str], cwd: Path, environment: Mapping[str, str], budget: float, log: Path) -> _RunResult:
    environment = temporary_environment(cwd, environment)
    try:
        with (
            atomic_files.stream(log, "wb") as output,
            atomic_files.stream(log.with_suffix(log.suffix + ".stderr"), "wb") as errors,
        ):
            if budget <= 0:
                return _RunResult(False, None)
            process = subprocess.Popen(
                command, cwd=cwd, env=environment, stdout=output, stderr=errors, start_new_session=True
            )
            # The group outlives a SIGKILLed owner unless something outside it is watching the owner.
            watcher = subprocess.Popen(
                [sys.executable, "-c", _WATCH, str(os.getpid()), str(process.pid)],
                env=environment,
                start_new_session=True,
            )
            try:
                status = process.wait(timeout=budget)
            except subprocess.TimeoutExpired:
                status = None
            finally:
                # Every exit, normal or not, ends the whole tree: the permuter's own workers and compilers.
                pool.kill_groups([process.pid])
                process.wait()
                watcher.kill()
                watcher.wait()
            if status is None:
                return _RunResult(True, None)
        return _RunResult(True, status)
    except OSError as error:
        raise Held(
            capture(
                error,
                cause=cause_named(
                    "search.permute._run",
                    f"permuter {shlex.join(command)}: {error}",
                    owner="search.permute",
                    stage="permute",
                ),
            )
        ) from error


def outputs(work: Path) -> Iterator[tuple[int, str]]:
    for directory in sorted(work.glob("output-*")):
        if not re.fullmatch(r"output-\d+-\d+", directory.name):
            raise Held(
                cause_named(
                    "search.permute.outputs",
                    f"result directory {directory.name}: expected output-SCORE-INDEX",
                    owner="search.permute",
                    stage="permute",
                )
            )
        if directory.is_symlink():
            raise Held(
                cause_named(
                    "search.permute.outputs",
                    f"result directory {directory}: symbolic link refused",
                    owner="search.permute",
                    stage="permute",
                )
            )
        try:
            score_path, source_path = directory / "score.txt", directory / "source.c"
            for path in (score_path, source_path):
                if path.is_symlink():
                    raise Held(
                        cause_named(
                            "search.permute.outputs",
                            f"result {path}: symbolic link refused",
                            owner="search.permute",
                            stage="permute",
                        )
                    )
            score = score_path.read_text().split()[0]
            if not re.fullmatch(r"\d+", score) or int(score) != int(directory.name.split("-")[1]):
                raise Held(
                    cause_named(
                        "search.permute.outputs",
                        f"result {score_path}: score disagrees with directory",
                        owner="search.permute",
                        stage="permute",
                    )
                )
            yield int(score), source_path.read_text(encoding="utf-8")
        except (OSError, UnicodeError, IndexError) as error:
            raise Held(
                capture(
                    error,
                    cause=cause_named(
                        "search.permute.outputs",
                        f"result {directory}: source.c and score.txt required: {error}",
                        owner="search.permute",
                        stage="permute",
                    ),
                )
            ) from error


@dataclass(frozen=True)
class Permuter:
    version: str
    target_object: Path
    budget_seconds: float
    ran: bool = field(default=False, init=False)

    def propose(self, source: str, trial: Compared, ctx: Context) -> Iterator[Mutation]:
        """Yield external improvements for the common search loop to confirm."""

        project = cast(Project, _required(ctx, "project", "context"))
        policy = cast(Host, _required(ctx, "policy", "context"))
        out = _outside(project, _required(ctx, "out", "context"), "context.out")
        source_path = Path(_required(ctx, "source", "context"))
        version = _required(self, "version", "permuter")
        project.version(version)
        target = Path(_required(self, "target_object", "permuter"))
        budget = _required(self, "budget_seconds", "permuter")
        if type(budget) not in (int, float) or not math.isfinite(budget) or budget <= 0:
            raise Held(
                cause_named(
                    "search.permute.propose",
                    "permuter.budget_seconds must be positive and finite",
                    owner="search.permute",
                    stage="permute",
                )
            )
        remaining_deadline = _required(ctx, "deadline", "context")
        if type(remaining_deadline) not in (int, float) or not math.isfinite(remaining_deadline):
            raise Held(
                cause_named(
                    "search.permute.propose",
                    "context.deadline must be finite monotonic time",
                    owner="search.permute",
                    stage="permute",
                )
            )
        deadline = min(time.monotonic() + budget, remaining_deadline)
        if time.monotonic() >= deadline:
            return
        if not isinstance(source, str) or not source.strip():
            raise Held(
                cause_named("search.permute.propose", "source is required", owner="search.permute", stage="permute")
            )
        function = _required(trial, "function", "trial")
        if not re.fullmatch(r"[A-Za-z_]\w*", function) or source_path.stem != function or source_path.suffix != ".c":
            raise Held(
                cause_named(
                    "search.permute.propose",
                    "context.source must be named trial.function.c",
                    owner="search.permute",
                    stage="permute",
                )
            )
        if not _required(trial, "compares", "trial"):
            raise Held(
                cause_named(
                    "search.permute.propose",
                    "trial.compares must name holding VERSIONs",
                    owner="search.permute",
                    stage="permute",
                )
            )
        archive = _required(policy, "permuter_archive", "policy")
        digest = _required(policy, "permuter_sha256", "policy")
        cores = _required(policy, "cores", "policy")
        if type(cores) is not int or cores <= 0:
            raise Held(
                cause_named(
                    "search.permute.propose",
                    "policy.cores must be a positive integer",
                    owner="search.permute",
                    stage="permute",
                )
            )
        for dependency in ("pycparser", "toml"):
            if importlib.util.find_spec(dependency) is None:
                raise Held(
                    cause_named(
                        "search.permute.propose",
                        f"dependency {dependency}: missing from interpreter {sys.executable}",
                        owner="search.permute",
                        stage="permute",
                    )
                )
        compiler = project.compiler_for(source_path)
        family = toolchain.specification(compiler.id).permuter
        if not family:
            raise Held(
                cause_named(
                    f"compiler.{compiler.id}",
                    f"compiler.{compiler.id}: no registered scorer target",
                    owner="search.permute",
                    stage="permute",
                )
            )
        toolchain.verify(project.tools / compiler.id, toolchain.specification(compiler.id))
        try:
            if not target.is_file():
                raise Held(
                    cause_named(
                        "search.permute.propose",
                        f"permuter.target_object {target}: missing object",
                        owner="search.permute",
                        stage="permute",
                    )
                )
            out.mkdir(parents=True, exist_ok=True)
            work = Path(tempfile.mkdtemp(prefix="permute-", dir=out))
            entry = checkout(archive, digest, work)
            atomic_files.text(work / "base.c", source, encoding="utf-8")
            environment = dict(temporary_environment(work), PYTHONDONTWRITEBYTECODE="1", LC_ALL="C")
            atomic_files.copyfile(target, work / "target.o")
            atomic_files.text(work / "settings.toml", f'func_name = "{function}"\ncompiler_type = "{family}"\n')
            script = work / "compile.sh"
            atomic_files.text(script, compile_script(project, policy, source_path, version, work))
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
                raise Held(
                    cause_named(
                        "search.permute.propose",
                        f"permuter {entry} exited {result.returncode}: {stderr}; inspect {stderr_path}",
                        owner="search.permute",
                        stage="permute",
                    )
                )
            log = log_path.read_text(encoding="utf-8")
            baseline = re.findall(r"base score = (\d+)", log)
            if result.returncode is None and not baseline:
                return
            if len(baseline) != 1:
                raise Held(
                    cause_named(
                        "search.permute.propose",
                        "permuter.log base score is required",
                        owner="search.permute",
                        stage="permute",
                    )
                )
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
            raise Held(
                capture(
                    error,
                    cause=cause_named(
                        "search.permute.propose", f"search output: {error}", owner="search.permute", stage="permute"
                    ),
                )
            ) from error
