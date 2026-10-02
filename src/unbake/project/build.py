"""Run the standalone proof path and cache individual compiler objects."""

from __future__ import annotations

import fcntl
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
from collections.abc import Callable, Iterator, Sequence
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from pathlib import Path

from unbake.project import makefile
from unbake.project.config import Held, Policy, Project


@dataclass(frozen=True)
class BuildResult:
    version: str
    ok: bool
    sha1_line: str
    log: Path
    generation: Path


@contextmanager
def lock(project: Project) -> Iterator[None]:
    """Serialize project build writes with generation publication."""
    path = project.build / ".lock"
    with _lock(path):
        yield


_held_locks = threading.local()


@contextmanager
def _lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise Held("build", f"{path}: build lock must not be a symlink")
    key = (os.getpid(), path.resolve())
    held: set[tuple[int, Path]] = getattr(_held_locks, "paths", set())
    if key in held:
        yield
        return
    with path.open("a+b") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        _held_locks.paths = held | {key}
        try:
            yield
        finally:
            _held_locks.paths = held


def current_generation(project: Project, v: str) -> Path:
    project.version(v)
    link = project.build_link(v)
    if not link.is_symlink():
        raise Held("build", f"{link}: build generation symlink is missing")
    try:
        generation = link.resolve(strict=True)
    except OSError as error:
        raise Held("build", f"{link}: {error}") from error
    if not generation.is_dir():
        raise Held("build", f"{link}: generation {generation} is not a directory")
    return generation


@contextmanager
def pin(generation: Path) -> Iterator[Path]:
    """Keep a generation alive; use pin_current for published-generation reads."""
    with (generation / ".inuse").open("a+b") as stream:
        fcntl.flock(stream, fcntl.LOCK_SH)
        yield generation


@contextmanager
def pin_current(project: Project, v: str) -> Iterator[Path]:
    """Pin a published generation without taking the build writer lock."""
    while True:
        generation = current_generation(project, v)
        with ExitStack() as holds:
            try:
                holds.enter_context(pin(generation))
            except FileNotFoundError:
                # Collection won the race between resolving and opening .inuse.
                continue
            if project.build_link(v).resolve() != generation or not generation.is_dir():
                # Publication or collection won before the shared pin was held.
                continue
            yield generation
            return


def discard_generation(generation: Path) -> None:
    """Drop an abandoned generation only after all readers release their pins."""
    with _lock(generation.parent / ".lock"):
        if not generation.is_dir():
            return
        with (generation / ".inuse").open("a+b") as stream:
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return
            shutil.rmtree(generation, ignore_errors=True)


def build(
    project: Project, policy: Policy, versions: Sequence[str], *, tree: Path, generation_for: Callable[[str], Path]
) -> dict[str, BuildResult]:
    if not versions:
        raise Held("build", "versions is required")
    if not isinstance(policy.cores, int) or policy.cores < 1:
        raise Held("build", "policy.cores must be a positive integer")
    for v in versions:
        project.version(v)
    tree = Path(tree).resolve()
    if not (tree / "Makefile").is_file():
        raise Held("build", f"{tree / 'Makefile'} is missing")
    from unbake.project import toolchain

    for ident in project.compilers:
        toolchain.verify(tree / project.tools.relative_to(project.root) / ident, toolchain.specification(ident))
    results = {}
    generations = {v: Path(generation_for(v)).resolve() for v in versions}
    for generation in generations.values():
        generation.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="build-") as temporary:
        driver = Path(temporary) / "Makefile"
        lines = [".PHONY: all " + " ".join(versions), "all: " + " ".join(versions)]
        for v, generation in generations.items():
            command = "$(MAKE) -C " + makefile.shell_words([tree, f"VERSION={v}", "COMPARE=1", f"BUILD={generation}"])
            log_word = makefile.shell_words([generation / "build.log"])
            status_word = makefile.shell_words([generation / "build.exit"])
            lines.extend(
                [v + ":", f"\t+@{command} > {log_word} 2>&1; result=$$?; echo $$result > {status_word}; exit $$result"]
            )
        driver.write_text("\n".join(lines) + "\n")
        try:
            subprocess.run(["make", "-f", str(driver), f"-j{policy.cores}", "-k"], capture_output=True, text=True)
        except OSError as error:
            raise Held("build", f"make: {error}") from error
    for v, generation in generations.items():
        log = generation / "build.log"
        output = log.read_text() if log.exists() else ""
        status = generation / "build.exit"
        success = status.exists() and status.read_text().strip() == "0"
        rom = generation / f"{project.name}.{v}.z64"
        sha1_line = next(
            (line for line in reversed(output.splitlines()) if line in (f"{rom}: OK", f"{rom}: FAILED")), ""
        )
        results[v] = BuildResult(v, success and sha1_line == f"{rom}: OK", sha1_line, log, generation)
    return results


def _run(command: list[str], root: Path) -> bytes:
    try:
        completed = subprocess.run(command, cwd=root, capture_output=True)
    except OSError as error:
        raise Held("compile", f"{command[0]}: {error}") from error
    if completed.returncode:
        raise Held(
            "compile",
            f"{command[0]} exited {completed.returncode}: "
            + (completed.stdout + completed.stderr).decode(errors="replace"),
        )
    return completed.stdout


def preprocess_object(project: Project, policy: Policy, source: Path, v: str) -> bytes:
    """Use the object's ordinary recipe to inspect active external references."""
    flags = list(makefile.flags(project, v, source))
    compiler = project.compiler_for(source)
    if compiler.kind == "sn64":
        from unbake.project_tools.sn64_cc import partition_flags

        recipe = makefile.recipe(project)
        preprocess, _ = partition_flags(flags)
        cpp = makefile.host_executable(policy, recipe.cpp or "policy:cpp", "cpp")
        return _run([cpp, *recipe.cppflags, *preprocess, str(source)], project.root)
    return _run([str(compiler.cc), *[flag for flag in flags if flag != "-c"], "-E", str(source)], project.root)


def compile_objects(project: Project, policy: Policy, sources: Sequence[Path], v: str, out: Path) -> dict[str, str]:
    """Compile project sources in one interpreter through the content cache; name each failure.

    Objects land at out/<stem>.o. The result maps each failed source stem to its diagnostic.
    """
    from unbake.project import toolchain

    project.version(v)
    for ident in {project.compiler_for(source).id for source in sources}:
        toolchain.verify(project.tools / ident, toolchain.specification(ident))
    out = Path(out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    relative = [Path(source).resolve().relative_to(project.src) for source in sources]
    with tempfile.TemporaryDirectory(prefix=".compile-", dir=out) as temporary:
        work = Path(temporary)
        for name, content in makefile.helpers(project).items():
            (work / Path(name).name).write_text(content)
        shutil.copyfile(project.tools / "compiler.sha256", work / "compiler.sha256")
        root = project.src.relative_to(project.root)
        completed = subprocess.run(
            [
                sys.executable,
                str(work / "compile.py"),
                "--kind",
                "cc",
                "--recipe",
                str(work / "build.json"),
                "--version",
                v,
                "--unit",
                "batch",
                "--source",
                str(root),
                "--output",
                str(out),
                "--non-matching",
                "0",
                "--cache-root",
                str(policy.cache_root),
                "--batch",
                *(str(root / path) for path in relative),
            ],
            cwd=project.root,
            capture_output=True,
            text=True,
        )
    failures: dict[str, str] = {}
    current = None
    for line in (completed.stdout + completed.stderr).splitlines():
        match = re.match(rf"^{re.escape(str(root))}/([A-Za-z_]\w*)\.c: (.*)$", line)
        if match:
            current = match[1]
            failures[current] = match[2]
        elif current is not None and not line.startswith("HELD("):
            failures[current] += "\n" + line
    for path in relative:
        if path.stem not in failures and not (out / path.with_suffix(".o")).is_file():
            failures[path.stem] = (completed.stderr or completed.stdout).strip()[-400:] or "no object produced"
    return failures


def compile_object(
    project: Project, policy: Policy, source: Path, v: str, out: Path, *, non_matching: bool = False
) -> Path:
    project.version(v)
    source, out = Path(source).resolve(), Path(out).resolve()
    if not source.is_file():
        raise Held("compile", f"source {source} is missing")
    from unbake.project import toolchain

    compiler = project.compiler_for(source)
    toolchain.verify(project.tools / compiler.id, toolchain.specification(compiler.id))
    out.parent.mkdir(parents=True, exist_ok=True)
    unit = (
        source.relative_to(project.root)
        if source.is_relative_to(project.src)
        else project.src.relative_to(project.root) / (source.stem + ".c")
    )
    with tempfile.TemporaryDirectory(prefix=".compile-", dir=out.parent) as temporary:
        work = Path(temporary)
        for name, content in makefile.helpers(project).items():
            (work / Path(name).name).write_text(content)
        shutil.copyfile(project.tools / "compiler.sha256", work / "compiler.sha256")
        _run(
            [
                sys.executable,
                str(work / "compile.py"),
                "--kind",
                "cc",
                "--recipe",
                str(work / "build.json"),
                "--version",
                v,
                "--unit",
                str(unit),
                "--source",
                str(source),
                "--output",
                str(out),
                "--non-matching",
                "1" if non_matching else "0",
                "--cache-root",
                str(policy.cache_root),
            ],
            project.root,
        )
    if not out.is_file():
        raise Held("compile", f"[compilers.{compiler.id}].cc produced no object at {out}")
    return out
