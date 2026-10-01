from __future__ import annotations

import os
import re
import shutil
import subprocess
from collections.abc import Iterable
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

from unbake.decomp import drafts, needs
from unbake.layout import split, split_apply
from unbake.match import declarations
from unbake.match.common import (
    Attempt,
    Draft,
    held,
    read,
    relative,
    sha,
)
from unbake.project import build
from unbake.project.config import Policy, Project

# Retained trials and local environments are outputs, not cartridge build inputs.
_OUTPUTS = frozenset({"build", ".git", "artifacts"})


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
        return [name for name in names if Path(directory) == source and name in _OUTPUTS]

    shutil.copytree(source, destination, ignore=ignore, symlinks=True)


def fingerprint(root: Path) -> dict[str, str]:
    result = {}
    for directory, names, files in os.walk(root, followlinks=True):
        if Path(directory) == root:
            names[:] = [name for name in names if name not in _OUTPUTS | {"data"}]
        for name in files:
            path = Path(directory) / name
            result[str(path.relative_to(root))] = sha(read(path))
    return result


def generation(project: Project, version: str, current: Path) -> Path:
    parent = project.root / "build"
    number = 1
    prefix = version + "."
    for path in parent.iterdir():
        suffix = path.name.removeprefix(prefix)
        if path.name.startswith(prefix) and suffix.isdigit():
            number = max(number, int(suffix) + 1)
    while True:
        generation = parent / f"{version}.{number}"
        try:
            generation.mkdir()
            break
        except FileExistsError:
            number += 1
    try:
        result = subprocess.run(
            ["cp", "-a", "--reflink=auto", str(current) + "/.", str(generation)],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode:
            held(f"VERSION {version}: cp {current} to {generation}: {result.stderr.strip()}")
        return generation
    except BaseException:
        shutil.rmtree(generation, ignore_errors=True)
        raise


def attempt(
    project: Project, policy: Policy, base: Path, workspace: Path, current: dict[str, Path], candidates: list[Draft]
) -> Attempt:
    tree = workspace / uuid4().hex
    generations = {}
    try:
        shutil.copytree(base, tree, symlinks=True)
        staged_project = replace(
            project,
            root=tree,
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
                drafts.match_edits(
                    staged_project,
                    draft.function,
                    declarations.final_source(staged_project, draft.content.decode("utf-8")),
                    draft.versions,
                ),
            )
        affected = {v for edit in applied for v in edit.versions}
        versions = [v for v in project.versions if v in affected]
        for version in versions:
            generations[version] = generation(project, version, current[version])
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
        return Attempt(tree, generations, failures, applied, resolved, diagnostics)
    except BaseException:
        Attempt(tree, generations, []).discard()
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
