from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from collections.abc import Iterable
from contextlib import ExitStack
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

from unbake.decomp import needs, work
from unbake.layout import split, split_apply
from unbake.match import declarations
from unbake.match.common import (
    QUEUE_PATH,
    Attempt,
    Draft,
    held,
    read,
    relative,
    sha,
)
from unbake.project import build
from unbake.project.config import Held, Policy, Project

# Retained trials and local environments are outputs, not cartridge build inputs.
_OUTPUTS = frozenset({".git", "artifacts", ".unbake", ".splat", ".mypy_cache", ".ruff_cache", ".pytest_cache"})


def project_input(project: Project, path: Path) -> bool:
    """Classify a project-relative path for both staging and publication checks."""
    return (
        bool(path.parts)
        and path.parts[0] not in _OUTPUTS
        and not any(
            path.is_relative_to(root.relative_to(project.root))
            for root in (project.build, project.work, project.drafts)
        )
        and path != QUEUE_PATH
        and not any(part in {"__pycache__", ".venv", "venv"} for part in path.parts)
        and path.name != "clone-policy.toml"
        and path.suffix not in {".pyc", ".pyo"}
    )


def compare_failure(project: Project, version: str, result: build.BuildResult) -> str:
    cartridge = project.version(version)
    rom = result.generation / f"{project.name}.{version}.z64"
    if not rom.is_file():
        return f"VERSION {version}: produced ROM missing; {read(result.log).decode(errors='replace')[-2000:]}"
    expected, produced = read(cartridge.baserom), read(rom)
    if expected == produced:
        return f"VERSION {version}: ROM bytes agree; {read(result.log).decode(errors='replace')[-2000:]}"
    offset = next(
        (i for i, (left, right) in enumerate(zip(expected, produced, strict=False)) if left != right),
        min(len(expected), len(produced)),
    )
    _, lines, segments = split.layout(cartridge.split)
    owner = next(
        (row.path for segment in segments for row in segment.rows if row.start <= offset < split.end(row)),
        "outside split units",
    )
    if owner == "outside split units":
        # Top-level header and binary units have no code subsegment rows.
        boundaries = sorted(
            int(match[1], 0) for line in lines if (match := re.match(rf"\s*-\s*\[\s*({split.NUMBER})", line))
        )
        for line in lines:
            row = split.ROW.fullmatch(line)
            if row:
                start = int(row["start"], 0)
                stop = next((boundary for boundary in boundaries if boundary > start), len(expected))
                if start <= offset < stop:
                    owner = split.plain(row["path"])
                    break
    return (
        f"VERSION {version}: first differing ROM offset 0x{offset:X}; "
        f"expected {expected[offset : offset + 16].hex() or '<EOF>'}, "
        f"produced {produced[offset : offset + 16].hex() or '<EOF>'}; unit {owner}; "
        f"sizes {len(expected)}/{len(produced)}"
    )


def copy_tree(project: Project, source: Path, destination: Path) -> None:

    def ignore(directory: str, names: list[str]) -> list[str]:
        return [name for name in names if not project_input(project, (Path(directory) / name).relative_to(source))]

    shutil.copytree(source, destination, ignore=ignore, symlinks=True)


def fingerprint(project: Project, root: Path) -> dict[str, str]:
    result = {}
    for directory, names, files in os.walk(root, followlinks=True):
        parent = Path(directory).relative_to(root)
        names[:] = [name for name in names if project_input(project, parent / name)]
        for name in files:
            path = Path(directory) / name
            if project_input(project, path.relative_to(root)):
                result[str(path.relative_to(root))] = sha(read(path))
    return result


def generation(project: Project, version: str, current: Path, holds: ExitStack) -> Path:
    parent = project.build
    number = 1
    prefix = version + "."
    for path in parent.iterdir():
        suffix = path.name.removeprefix(prefix)
        if path.name.startswith(prefix) and suffix.isdigit():
            number = max(number, int(suffix) + 1)
    with build.lock(project):
        while True:
            generation = parent / f"{version}.{number}"
            try:
                generation.mkdir()
                holds.enter_context(build.pin(generation))
                break
            except FileExistsError:
                number += 1
    try:
        result = subprocess.run(
            ["cp", "-a", "--reflink=auto", *(str(p) for p in current.iterdir() if p.name != ".inuse"), str(generation)],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode:
            held(f"VERSION {version}: cp {current} to {generation}: {result.stderr.strip()}")
        return generation
    except BaseException:
        holds.close()
        build.discard_generation(generation)
        raise


def chunk_stale_sources(generation: Path, tools: Path) -> None:
    """Let the ordinary Make cold-chunk rule refresh outdated C receipts.

    Keep objects and dependency files: the compiler still verifies their content
    keys, while Make avoids starting one interpreter per stale source receipt.
    """
    recipe = tools / "build.json"
    drivers = tuple(tools.glob("*.py"))
    inputs = [path for path in (recipe, *drivers) if path.is_file()]
    if not inputs:
        return
    newest = max(path.stat().st_mtime_ns for path in inputs)
    for receipt in (generation / "obj" / "src").rglob("*.built"):
        if not receipt.is_symlink() and receipt.stat().st_mtime_ns < newest:
            receipt.unlink()


def project_at(project: Project, tree: Path) -> Project:
    """Relocate mutable build inputs to an isolated tree."""

    def relocated(path: Path) -> Path:
        return tree / path.relative_to(project.root) if path.is_relative_to(project.root) else path

    return replace(
        project,
        root=tree,
        tools=relocated(project.tools),
        asm=relocated(project.asm),
        roms=relocated(project.roms),
        build=relocated(project.build),
        work=relocated(project.work),
        drafts=relocated(project.drafts),
        compilers={
            ident: replace(
                compiler, cc=relocated(compiler.cc), as_=relocated(compiler.as_), sha256=relocated(compiler.sha256)
            )
            for ident, compiler in project.compilers.items()
        },
        src=tree / relative(project, project.src),
        include=tuple(tree / relative(project, path) for path in project.include),
        version_map={
            v: replace(
                project.version(v),
                baserom=relocated(project.version(v).baserom),
                split=tree / relative(project, project.version(v).split),
                symbols=tree / relative(project, project.version(v).symbols),
            )
            for v in project.versions
        },
    )


def attempt(
    project: Project, policy: Policy, base: Path, workspace: Path, current: dict[str, Path], candidates: list[Draft]
) -> Attempt:
    tree = workspace / uuid4().hex
    generations = {}
    holds = ExitStack()
    try:
        shutil.copytree(base, tree, symlinks=True)
        # Clone Make graphs select this runtime configuration relative to their
        # build tree. Supply it without treating policy/cache state as inputs.
        local_policy = project.tools / "clone-policy.toml"
        if local_policy.is_file():
            shutil.copy2(local_policy, tree / relative(project, local_policy))
        staged_project = project_at(project, tree)
        applied: list[split.Edit] = []

        def apply(staged: Project, policy: Policy, edits: Iterable[split.Edit]) -> None:
            edits = list(edits)
            write_staged(staged, edits)
            applied.extend(edits)

        resolved = needs.resolve([need for draft in candidates for need in draft.needs], staged_project, policy, apply)
        for draft in candidates:
            manifest = draft.row["work"]
            overlays = [
                replace(edit, path=tree / relative(project, edit.path)) for edit in work.header_edits(project, manifest)
            ]
            apply(staged_project, policy, overlays)
            apply(
                staged_project,
                policy,
                declarations.folded_edits(
                    staged_project, policy, draft.function, draft.content.decode("utf-8"), draft.versions
                ),
            )
        affected = {v for edit in applied for v in edit.versions}
        versions = [v for v in project.versions if v in affected]
        for version in versions:
            generations[version] = generation(project, version, current[version], holds)
            chunk_stale_sources(generations[version], tree / relative(project, project.tools))
        results = build.build(project, policy, versions, tree=tree, generation_for=generations.__getitem__)
        failures = []
        diagnostics = {}
        for version in versions:
            if version not in results:
                held(f"build.build: missing VERSION {version} result")
            result = results[version]
            if not isinstance(result.ok, bool):
                held(f"build.build VERSION {version}: missing ok boolean")
            if result.generation.resolve() != generations[version].resolve():
                held(f"build.build VERSION {version}: unexpected generation {result.generation}")
            if not result.ok:
                failures.append(version)
                diagnostics[version] = f"submit.sha1.{version}: " + compare_failure(staged_project, version, result)
        return Attempt(
            tree,
            generations,
            failures,
            applied,
            resolved,
            diagnostics,
            holds,
            {v: results[v].sha1_line for v in versions if results[v].ok},
        )
    except BaseException:
        Attempt(tree, generations, [], holds=holds).discard()
        raise


def bisect(
    project: Project,
    policy: Policy,
    base: Path,
    workspace: Path,
    current: dict[str, Path],
    group: list[Draft],
    accepted: list[Draft],
    receipts: list[str],
) -> list[Draft]:
    result = attempt(project, policy, base, workspace, current, accepted + group)
    failures = result.failures
    detail = "; ".join(result.diagnostics.get(v, f"VERSION {v}") for v in failures)
    result.discard()
    if not failures:
        return accepted + group
    if len(group) == 1:
        receipts.append(f"HELD(match): {group[0].function}: build compare failed on {detail}")
        return accepted
    middle = len(group) // 2
    accepted = bisect(project, policy, base, workspace, current, group[:middle], accepted, receipts)
    return bisect(project, policy, base, workspace, current, group[middle:], accepted, receipts)


def compile_fold(project: Project, policy: Policy, draft: Draft) -> None:
    """Compile the publication form before writing any queue state."""
    project.work.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="match-submit-", dir=project.work) as temporary:
        workspace = Path(temporary)
        tree = workspace / "tree"
        copy_tree(project, project.root, tree)
        staged = project_at(project, tree)
        overlays = [
            replace(edit, path=tree / relative(project, edit.path))
            for edit in work.header_edits(project, draft.row["work"])
        ]
        write_staged(staged, overlays)
        edits = declarations.folded_edits(staged, policy, draft.function, draft.content.decode("utf-8"), draft.versions)
        write_staged(staged, edits)
        for version in draft.versions:
            try:
                build.compile_object(
                    staged,
                    policy,
                    staged.src / f"{draft.function}.c",
                    version,
                    workspace / version / f"{draft.function}.o",
                )
            except (Held, OSError) as error:
                held(f"{draft.function}: folded source compile failed on VERSION {version}: {error}")


def write_staged(project: Project, edits: Iterable[split.Edit]) -> None:
    """Apply pre-proof edits only inside a private tree, with rollback on error."""
    split_apply._write_staging(project, edits)
