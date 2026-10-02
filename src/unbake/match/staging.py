from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from collections import ChainMap
from collections.abc import Iterable, Mapping
from contextlib import ExitStack
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

from unbake.decomp import needs
from unbake.layout import split, split_apply
from unbake.match import data_symbols, declarations
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
_OUTPUTS = frozenset({"build", ".git", "artifacts", ".unbake", ".splat", ".mypy_cache", ".ruff_cache", ".pytest_cache"})


def project_input(path: Path) -> bool:
    """Classify a project-relative path for both staging and publication checks."""
    return (
        bool(path.parts)
        and path.parts[0] not in _OUTPUTS
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


def copy_tree(source: Path, destination: Path) -> None:

    def ignore(directory: str, names: list[str]) -> list[str]:
        return [name for name in names if not project_input((Path(directory) / name).relative_to(source))]

    shutil.copytree(source, destination, ignore=ignore, symlinks=True)


def fingerprint(root: Path) -> dict[str, str]:
    result = {}
    for directory, names, files in os.walk(root, followlinks=True):
        parent = Path(directory).relative_to(root)
        names[:] = [name for name in names if project_input(parent / name)]
        for name in files:
            path = Path(directory) / name
            if project_input(path.relative_to(root)):
                result[str(path.relative_to(root))] = sha(read(path))
    return result


def generation(project: Project, version: str, current: Path, holds: ExitStack) -> Path:
    parent = project.root / "build"
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
                split=tree / relative(project, project.version(v).split),
                symbols=tree / relative(project, project.version(v).symbols),
            )
            for v in project.versions
        },
    )


def attempt(
    project: Project, policy: Policy, base: Path, workspace: Path, current: Mapping[str, Path], candidates: list[Draft]
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
            split_apply.apply(staged, policy, edits, staged=True)
            applied.extend(edits)

        resolved = needs.resolve([need for draft in candidates for need in draft.needs], staged_project, policy, apply)
        for draft in candidates:
            apply(
                staged_project,
                policy,
                declarations.folded_edits(
                    staged_project, policy, draft.function, draft.content.decode("utf-8"), draft.versions
                ),
            )
            for version in draft.versions:
                apply(
                    staged_project,
                    policy,
                    data_symbols.prepare(
                        staged_project,
                        policy,
                        draft.function,
                        version,
                        tree / ".unbake" / f"{draft.function}.{version}.o",
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
                diagnostics[version] = compare_failure(staged_project, version, result)
        return Attempt(tree, generations, failures, applied, resolved, diagnostics, holds)
    except BaseException:
        Attempt(tree, generations, [], holds=holds).discard()
        raise


def isolate(
    project: Project,
    policy: Policy,
    base: Path,
    workspace: Path,
    current: dict[str, Path],
    candidates: list[Draft],
    result: Attempt,
    receipts: list[str],
) -> tuple[Attempt | None, list[Draft]]:
    """Halve a failed set, then verify its remainder using the preceding build.

    One failing item costs logarithmically many builds. A failed remainder starts
    another isolation pass, preserving support for multiple failures/interactions.
    Only the preceding build and latest passing subset stay pinned.
    """
    passing: Attempt | None = None
    passing_names: tuple[str, ...] = ()

    def names(group: list[Draft]) -> tuple[str, ...]:
        return tuple(draft.function for draft in group)

    def test(group: list[Draft]) -> Attempt:
        nonlocal result, passing, passing_names
        if passing is not None and names(group) == passing_names:
            if result is not passing:
                result.discard()
            result = passing
            return result
        previous = result
        result = attempt(project, policy, base, workspace, ChainMap(previous.generations, current), group)
        if previous is not passing:
            previous.discard()
        if not result.failures:
            if passing is not None and passing is not result:
                passing.discard()
            passing, passing_names = result, names(group)
        return result

    try:
        while result.failures and candidates:
            suspect = list(candidates)
            accepted: list[Draft] = []
            detail = "; ".join(result.diagnostics.values())
            while len(suspect) > 1:
                middle = len(suspect) // 2
                left, right = suspect[:middle], suspect[middle:]
                tested = test(accepted + left)
                if tested.failures:
                    detail = "; ".join(tested.diagnostics.values())
                    suspect = left
                else:
                    accepted += left
                    suspect = right
            bad = suspect[0]
            receipts.append(f"HELD(match): {bad.function}: build compare failed on {detail}")
            candidates = [draft for draft in candidates if draft is not bad]
            if not candidates:
                result.discard()
                if passing is not None and passing is not result:
                    passing.discard()
                return None, []
            test(candidates)
        if passing is not None and passing is not result:
            passing.discard()
        return result, candidates
    except BaseException:
        result.discard()
        if passing is not None and passing is not result:
            passing.discard()
        raise


def compile_fold(project: Project, policy: Policy, draft: Draft) -> None:
    """Validate folded declarations and unresolved data before writing queue state."""
    with tempfile.TemporaryDirectory(prefix="match-submit-") as temporary:
        workspace = Path(temporary)
        tree = workspace / "tree"
        copy_tree(project.root, tree)
        staged = project_at(project, tree)
        edits = declarations.folded_edits(staged, policy, draft.function, draft.content.decode("utf-8"), draft.versions)
        split_apply.apply(staged, policy, edits, staged=True)
        for version in draft.versions:
            try:
                data_symbols.prepare(
                    staged, policy, draft.function, version, workspace / version / f"{draft.function}.o"
                )
            except (Held, OSError) as error:
                held(f"{draft.function}: folded source compile failed on VERSION {version}: {error}")
