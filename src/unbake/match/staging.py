from __future__ import annotations

import json
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

from unbake.decomp import checks, drafts, needs, work
from unbake.layout import split, split_apply
from unbake.match import attribution, data_symbols, declarations, relink, reporting
from unbake.match.common import (
    QUEUE_PATH,
    Attempt,
    Draft,
    held,
    read,
    relative,
    sha,
)
from unbake.project import build, compiler_ties, config, makefile
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
    """Let the ordinary Make cold-chunk rule refresh outdated C/assembly receipts.

    Keep objects and dependency files: the compiler still verifies their content
    keys, while Make avoids starting one interpreter per stale source receipt.
    """
    recipe = tools / "build.json"
    drivers = tuple(tools.glob("*.py"))
    inputs = [path for path in (recipe, *drivers) if path.is_file()]
    if not inputs:
        return
    newest = max(path.stat().st_mtime_ns for path in inputs)
    for kind in ("src", "asm"):
        for receipt in (generation / "obj" / kind).rglob("*.built"):
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


def source_edits(project: Project, policy: Policy, draft: Draft, *, prove_headers: bool = True) -> list[split.Edit]:
    options = {} if prove_headers else {"prove_headers": False}
    edits = declarations.folded_edits(
        project, policy, draft.function, draft.content.decode("utf-8"), draft.versions, **options
    )
    source = next(edit for edit in edits if edit.path == project.src / (draft.function + ".c"))
    blockers = [finding for finding in checks.run(source.after) if finding.fakematch is None]
    if blockers:
        held("submit.source_rules: " + "; ".join(checks.message(finding) for finding in blockers))
    if draft.matched:
        return edits
    final = drafts.canonical_source(source.after.encode()).decode()
    guarded = "#ifdef NON_MATCHING\n" + final.rstrip("\n") + "\n#endif\n"
    return [
        *(edit for edit in edits if any(edit.path.is_relative_to(root) for root in project.include)),
        replace(source, after=guarded),
    ]


def helper_edits(project: Project) -> list[split.Edit]:
    """Stage current generated drivers with verified checksum replacements."""
    edits = []
    for relative_path, content in makefile.helpers(project).items():
        path = project.root / relative_path
        if path.suffix == ".py" and (before := path.read_text() if path.exists() else "") != content:
            edits.append(split.Edit(path, before, content, project.versions))
    if not edits:
        return []
    checksum = project.tools / "compiler.sha256"
    before_checksum = checksum.read_text()
    lines = before_checksum.splitlines(keepends=True)
    for edit in edits:
        name = edit.path.relative_to(project.root).as_posix()
        entries = [i for i, line in enumerate(lines) if line.strip().split(maxsplit=1)[1:] == [name]]
        if edit.path.exists():
            if len(entries) != 1 or lines[entries[0]].split()[0] != sha(edit.before.encode()):
                held(f"submit.helper_checksum: {name}: expected one verified generated helper entry")
            lines[entries[0]] = f"{sha(edit.after.encode())}  {name}\n"
        elif entries:
            held(f"submit.helper_checksum: {name}: declared generated helper is missing")
        else:
            lines.append(f"{sha(edit.after.encode())}  {name}\n")
    return [*edits, split.Edit(checksum, before_checksum, "".join(lines), project.versions)]


def compiler_edits(project: Project, candidates: list[Draft]) -> tuple[Project, list[split.Edit]]:
    """Render all requested compiler decisions into the private proof tree once."""
    import toml  # type: ignore[import-untyped]

    receipts = [
        draft.row["work"]["compiler_evidence"] for draft in candidates if draft.row["work"]["compiler_evidence"]
    ]
    if not receipts:
        return project, []
    path = project.root / "config.toml"
    before = path.read_text()
    after = toml.dumps(compiler_ties.fold(project, receipts))
    config_edit = split.Edit(path, before, after, project.versions)
    write_staged(project, [config_edit])
    project = config.load(project.root)
    recipe = project.tools / "build.json"
    rendered = json.dumps(makefile.description(project), sort_keys=True, indent=2) + "\n"
    recipe_edit = split.Edit(recipe, recipe.read_text(), rendered, project.versions)
    checksum = project.tools / "compiler.sha256"
    relative_recipe = recipe.relative_to(project.root).as_posix()
    lines = checksum.read_text().splitlines(keepends=True)
    entries = [i for i, line in enumerate(lines) if line.strip().split(maxsplit=1)[1:] == [relative_recipe]]
    if len(entries) != 1 or lines[entries[0]].split()[0] != sha(recipe.read_bytes()):
        held("submit.compiler_recipe: expected one verified build recipe manifest entry")
    lines[entries[0]] = f"{sha(rendered.encode())}  {relative_recipe}\n"
    checksum_edit = split.Edit(checksum, checksum.read_text(), "".join(lines), project.versions)
    edits = [config_edit, recipe_edit, checksum_edit]
    write_staged(project, edits[1:])
    return project, edits


def attempt(
    project: Project,
    policy: Policy,
    base: Path,
    workspace: Path,
    current: Mapping[str, Path],
    candidates: list[Draft],
    *,
    reuse: Attempt | None = None,
) -> Attempt:
    tree = workspace / uuid4().hex
    generations: dict[str, Path] = {}
    holds = ExitStack()
    active: Draft | None = None
    refused: dict[str, str] = {}
    try:
        shutil.copytree(base, tree, symlinks=True)
        # Clone Make graphs select this runtime configuration relative to their
        # build tree. Supply it without treating policy/cache state as inputs.
        local_policy = project.tools / "clone-policy.toml"
        if local_policy.is_file():
            shutil.copy2(local_policy, tree / relative(project, local_policy))
        staged_project = project_at(project, tree)
        drivers = helper_edits(staged_project)
        write_staged(staged_project, drivers)
        staged_project, applied = compiler_edits(staged_project, candidates)
        applied = drivers + applied
        from unbake.decomp import exclusions

        compiler_changes = list(applied[len(drivers) :])
        compiler_count = len(applied)

        def apply(staged: Project, policy: Policy, edits: Iterable[split.Edit]) -> None:
            edits = list(edits)
            write_staged(staged, edits)
            applied.extend(edits)

        resolved: list[str] = []
        accepted = []
        for draft in candidates:
            active = draft
            checkpoint = len(applied)
            needs_checkpoint = len(resolved)
            try:
                resolved.extend(needs.resolve(draft.needs, staged_project, policy, apply))
                manifest = draft.row["work"]
                overlays = []
                for edit in work.header_edits(project, manifest):
                    path = tree / relative(project, edit.path)
                    # The first identical overlay owns the shared edit.
                    if path.is_file() and path.read_text() == edit.after:
                        continue
                    overlays.append(replace(edit, path=path))
                apply(staged_project, policy, overlays)
                apply(
                    staged_project,
                    policy,
                    source_edits(staged_project, policy, draft, prove_headers=reuse is None),
                )
                for version in draft.versions if draft.matched else ():
                    apply(
                        staged_project,
                        policy,
                        (
                            data_symbols.edits(staged_project, policy, draft.function, version, cached)
                            if reuse is not None
                            and (cached := reuse.tree / ".unbake" / f"{draft.function}.{version}.o").is_file()
                            else []
                            if reuse is not None
                            else data_symbols.prepare(
                                staged_project,
                                policy,
                                draft.function,
                                version,
                                tree / ".unbake" / f"{draft.function}.{version}.o",
                            )
                        ),
                    )
                accepted.append(draft)
            except Held as error:
                detail = f"HELD(match): {draft.function}: submit.preparation: {error.reason}"
                refused[draft.function] = detail
                reporting.learn(detail)
                for edit in reversed(applied[checkpoint:]):
                    if edit.before:
                        split_apply.write(edit.path, edit.before)
                    else:
                        edit.path.unlink(missing_ok=True)
                del applied[checkpoint:]
                del resolved[needs_checkpoint:]
        candidates = accepted
        active = None
        if refused:
            # Remove choices for refused sources before proving or publishing.
            for edit in reversed(compiler_changes):
                split_apply.write(edit.path, edit.before)
            staged_project = project_at(project, tree)
            staged_project, compiler_changes = compiler_edits(staged_project, candidates)
            applied = drivers + compiler_changes + applied[compiler_count:]
        excluded = exclusions.publication_edit(staged_project, {draft.function for draft in candidates})
        write_staged(staged_project, excluded)
        applied.extend(excluded)
        if not candidates:
            return Attempt(tree, generations, [], holds=holds, refused=refused)
        active = None
        affected = {v for edit in applied for v in edit.versions}
        versions = [v for v in project.versions if v in affected]
        for version in versions:
            generations[version] = generation(project, version, current[version], holds)
            if reuse is None:
                chunk_stale_sources(generations[version], tree / relative(project, project.tools))
        if reuse is None:
            results = build.build(project, policy, versions, tree=tree, generation_for=generations.__getitem__)
            object_inputs = relink.inputs(staged_project, policy, versions, generations)
        else:
            object_inputs = reuse.object_inputs
            results = build.relink(
                project,
                policy,
                versions,
                tree=tree,
                generation_for=generations.__getitem__,
                object_inputs=object_inputs,
            )
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
        reporting.record(
            "proof",
            mode="relink" if reuse is not None else "build",
            sources=[draft.function for draft in candidates],
            failures=diagnostics,
            generations={v: str(g) for v, g in generations.items()},
        )
        outcome = Attempt(
            tree,
            generations,
            failures,
            applied,
            resolved,
            diagnostics,
            holds,
            {v: results[v].sha1_line for v in versions if results[v].ok},
            object_inputs,
            refused=refused,
        )
        outcome.culprits = attribution.diagnose(staged_project, outcome, candidates)
        reporting.record("attribution", culprits=outcome.culprits)
        return outcome
    except Held as error:
        reporting.record("preparation", sources=[draft.function for draft in candidates], reason=error.reason)
        return Attempt(
            tree,
            generations,
            ["preparation"],
            diagnostics={"preparation": error.reason},
            holds=holds,
            culprits={active.function: [error.reason]} if active is not None else {},
        )
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
    """Remove attributed faults together; unresolved interactions use retained objects."""
    pool = result
    passing: Attempt | None = None
    passing_names: tuple[str, ...] = ()

    def names(group: list[Draft]) -> tuple[str, ...]:
        return tuple(draft.function for draft in group)

    def test(group: list[Draft]) -> Attempt:
        nonlocal result, passing, passing_names, pool, candidates
        group = [draft for draft in group if draft in candidates]
        if passing is not None and names(group) == passing_names:
            if result is not passing and result is not pool:
                result.discard()
            result = passing
            return result
        previous = result
        result = attempt(
            project,
            policy,
            base,
            workspace,
            ChainMap(pool.generations, current),
            group,
            reuse=pool if pool.generations else None,
        )
        if result.refused:
            receipts.extend(result.refused.values())
            candidates = [draft for draft in candidates if draft.function not in result.refused]
            group = [draft for draft in group if draft.function not in result.refused]
        if previous is not passing and previous is not pool:
            previous.discard()
        # Preparation failed before any objects existed: the first actual build
        # establishes the pool; later isolation must never compile again.
        if not pool.generations and result.generations:
            pool.discard()
            pool = result
        if not result.failures:
            if passing is not None and passing is not result and passing is not pool:
                passing.discard()
            passing, passing_names = result, names(group)
        return result

    try:
        while result.failures and candidates:
            faults = {name: detail for name, detail in result.culprits.items() if name in names(candidates)}
            if faults:
                for draft in candidates:
                    if draft.function in faults:
                        receipts.append(
                            f"HELD(match): {draft.function}: build compare failed on "
                            + "; ".join(faults[draft.function])
                        )
                candidates = [draft for draft in candidates if draft.function not in faults]
            else:
                suspect = list(candidates)
                accepted: list[Draft] = []
                detail = "; ".join(result.diagnostics.values())
                if not pool.generations:
                    for draft in candidates:
                        receipts.append(f"HELD(match): {draft.function}: {detail}")
                    candidates = []
                else:
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
                return None, []
            test(candidates)
        return (result, candidates) if candidates else (None, [])
    except BaseException:
        result.discard()
        raise
    finally:
        for abandoned in (pool, passing):
            if abandoned is not None and abandoned is not result:
                abandoned.discard()
        if not candidates:
            result.discard()


def compile_fold(project: Project, policy: Policy, draft: Draft) -> None:
    """Compile the publication form before writing any queue state."""
    project.work.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="match-submit-", dir=project.work) as temporary:
        workspace = Path(temporary)
        tree = workspace / "tree"
        copy_tree(project, project.root, tree)
        staged = project_at(project, tree)
        staged, _ = compiler_edits(staged, [draft])
        overlays = [
            replace(edit, path=tree / relative(project, edit.path))
            for edit in work.header_edits(project, draft.row["work"])
        ]
        write_staged(staged, overlays)
        edits = source_edits(staged, policy, draft)
        write_staged(staged, edits)
        for version in draft.versions:
            try:
                output = workspace / version / f"{draft.function}.o"
                if draft.matched:
                    data_symbols.prepare(staged, policy, draft.function, version, output)
                else:
                    build.compile_object(
                        staged, policy, staged.src / (draft.function + ".c"), version, output, non_matching=True
                    )
            except (Held, OSError) as error:
                held(f"{draft.function}: folded source compile failed on VERSION {version}: {error}")


def write_staged(project: Project, edits: Iterable[split.Edit]) -> None:
    """Apply the complete publication edits only inside its private proof tree."""
    edits = split_apply.coalesce(edits)
    configured = {
        project.root / "config.toml",
        project.root / "unbake-exclusions.json",
        project.tools / "build.json",
        project.tools / "compiler.sha256",
        *(
            p
            for version in project.versions
            for p in (project.version(version).split, project.version(version).symbols)
        ),
        *(project.tools / name.name for name in makefile.TEMPLATES.glob("*.py")),
        project.tools / "cache.py",
    }
    for edit in edits:
        relative(project, edit.path)
        if edit.path not in configured and not any(
            edit.path.is_relative_to(root) for root in (project.src, *project.include)
        ):
            held(f"{edit.path}: outside publication inputs")
        if (edit.path.read_text() if edit.path.exists() else "") != edit.before:
            prerequisite = (
                "headers.declaration: " if any(edit.path.is_relative_to(root) for root in project.include) else ""
            )
            held(f"{prerequisite}{edit.path}: changed since publication preview")
        for version in edit.versions:
            project.version(version)
    written = []
    try:
        for edit in edits:
            exists = edit.path.exists()
            split_apply.write(edit.path, edit.after)
            written.append((edit, exists))
    except BaseException:
        for edit, exists in reversed(written):
            if exists:
                split_apply.write(edit.path, edit.before)
            else:
                edit.path.unlink(missing_ok=True)
        raise
