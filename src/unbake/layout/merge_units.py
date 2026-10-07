"""The `merge-units` step: try one merged C unit for each new run of adjacent matched members in a group.

A run is two or more consecutive members of one layout.toml group that are all published (src/NAME.c), whose
rows are contiguous in every holding version, held by the same versions, built by the same compiler and not
separated by a recorded `split` cut. The merged source is src/<first>.c: the union of the members' include
lines, then their bodies in address order. It is proved like a land: in every holding version the strict
`n64link place` link of the merged unit must equal the ROM bytes of the whole run.
- Every run is proved independently in the shared worker pool; one writer then applies the results in layout
  order and makes one commit for the pass.
- Pass: write the merged source, remove the others, absorb their split rows into the first row, drop them from
  the group (with their `only` marks and cuts) and mark the group `evidence = "proven"`, in one layout edit.
- Fail: record every member after the first as a split cut, so the run is never tried again until someone
  removes the cut (the evidence changed).
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from dataclasses import replace
from pathlib import Path

from unbake import atomic as atomic_files
from unbake import cache, inputs, runner, scratch
from unbake.config import Held, Host, Project
from unbake.layout import map as layout_map
from unbake.layout import split
from unbake.process import named as cause_named
from unbake.work import compare

# Bump when this step's output changes for the same inputs. Keys never digest the tool's code.
SCHEMA = 3

_INCLUDE = re.compile(r"^[ \t]*#[ \t]*include[^\n]*\n?", re.M)


def input_key(project: Project) -> str:
    paths = (
        project.root / "layout.toml",
        *sorted(project.src.glob("*.c")),
        *(project.version(version).split for version in project.versions),
    )
    dependencies = inputs.DependencySet(
        tuple(inputs.file_pin(path, root=project.root, root_id="project", reuse=cache.configured()) for path in paths),
        {"versions": list(project.versions)},
        {"merge-inputs": inputs.digest(Path(__file__), algorithm="sha256", reuse=cache.configured())},
    )
    return cache.key("merge-units", str(SCHEMA), dependencies.digest)


def runs(project: Project) -> list[tuple[layout_map.Group, tuple[str, ...]]]:
    """Mergeable runs, in layout order."""
    landed = {path.stem for path in project.src.glob("*.c")}
    if len(landed) < 2:
        return []
    owners = _owners(project)
    return member_runs(
        layout_map.load(project).groups, landed, lambda left, right: _joins(project, owners, left, right)
    )


Owners = dict[str, dict[str, list[split.Function]]]


def _owners(project: Project) -> Owners:
    """Each version's rows by alias, read once per pass (a per-pair lookup re-hashes every row of the split)."""
    return {v: split.owners_by_alias(project, v) for v in project.versions}


def _row(owners: Owners, function: str, version: str) -> split.Function:
    rows = owners[version].get(function, [])
    if len(rows) != 1:
        raise Held(
            cause_named(
                "compare.row",
                f"compare.row: {function}: expected one row in VERSION {version}, found {len(rows)}",
                owner="layout.merge_units",
                stage="compare",
            )
        )
    return rows[0]


def member_runs(
    groups: Iterable[layout_map.Group], landed: set[str], joins: Callable[[str, str], bool]
) -> list[tuple[layout_map.Group, tuple[str, ...]]]:
    """Maximal runs (two or more) of adjacent landed members of one group; a split member starts a new run."""
    found = []
    for group in groups:
        current: list[str] = []
        for member in (*group.members, None):
            extends = member is not None and member in landed and member not in group.split
            if member is not None and extends and current and joins(current[-1], member):
                current.append(member)
                continue
            if len(current) >= 2:
                found.append((group, tuple(current)))
            current = [member] if member is not None and member in landed else []
    return found


def _joins(project: Project, owners: Owners, left: str, right: str) -> bool:
    versions = split.holding_versions(project, left, owners)
    if versions != split.holding_versions(project, right, owners):
        return False
    if project.compiler_reference(left) != project.compiler_reference(right):
        return False
    return all(
        _row(owners, left, v).kind == _row(owners, right, v).kind == "c"
        and _row(owners, left, v).end == _row(owners, right, v).start
        for v in versions
    )


def merged_source(project: Project, members: tuple[str, ...]) -> str:
    includes: list[str] = []
    bodies: list[str] = []
    for member in members:
        text = (project.src / f"{member}.c").read_text()
        for line in _INCLUDE.findall(text):
            line = line.rstrip("\n") + "\n"
            if line not in includes:
                includes.append(line)
        bodies.append(_INCLUDE.sub("", text).strip("\n") + "\n")
    return "".join(includes) + "\n" + "\n".join(bodies)


def prove(
    project: Project, host: Host, members: tuple[str, ...], source: str, *, versions: tuple[str, ...] | None = None
) -> bool:
    first = members[0]
    versions = split.holding_versions(project, first) if versions is None else versions
    if not versions:
        return False
    with scratch.temporary(host, project, "merge", prefix="merge-") as temporary:
        work = Path(temporary)
        file = work / f"{first}.c"
        atomic_files.text(file, source)
        for version in versions:
            head = compare.row_of(project, first, version)
            tail = compare.row_of(project, members[-1], version)
            row = replace(head, end=tail.end)
            try:
                with runner.compile_unit(project, host, file, version, unit=first) as obj:
                    placed = work / f"{version}.placed.o"
                    runner.place(project, host, obj, version, row, placed, score=False)
                    linked = runner.link(project, host, placed, version, row, work, file)
            except Held:
                raise
            if linked != split.words(project, row):
                return False
    return True


def _absorb_rows(project: Project, starts: dict[str, set[int]]) -> list[Path]:
    """Remove the code rows starting at `starts` (absorbed members), one rewrite per version."""
    changed = []
    for version, absorbed in starts.items():
        path = project.version(version).split
        text, lines, segments = split.layout(path)
        drop = {row.line for segment in segments for row in segment.rows if row.start in absorbed and row.kind == "c"}
        after = "".join(line for index, line in enumerate(lines) if index not in drop)
        if after != text:
            atomic_files.text(path, after)
            changed.append(path)
    return changed


def prove_job(job: tuple[Project, Host, tuple[str, ...], str]) -> bool:
    """Worker body: one run's proof in every holding version."""
    return prove(*job)


def _run(project: Project, host: Host) -> list[str]:
    """Prove every run in the worker pool, then write and commit the results in layout order; one line per run."""
    from unbake import buildfiles, land, pool
    from unbake import config as project_config

    found = runs(project)
    sources = [merged_source(project, members) for _, members in found]
    proven = pool.run(
        host, prove_job, [(project, host, members, src) for (_, members), src in zip(found, sources, strict=True)]
    )
    if not found:
        return []
    layout = project.root / "layout.toml"
    lines = []
    touched: list[Path] = [layout]
    absorbed: dict[str, tuple[str, ...]] = {}
    starts: dict[str, set[int]] = {}
    firsts: list[str] = []
    owners = _owners(project)
    from unbake import journal

    with journal.transaction(project):
        for (group, members), source, passed in zip(found, sources, proven, strict=True):
            if not passed:
                lines.append(f"merge {group.name} {members[0]}..{members[-1]}: refused; byte mismatch")
                continue
            for version in split.holding_versions(project, members[0], owners):
                starts.setdefault(version, set()).update(_row(owners, m, version).start for m in members[1:])
            atomic_files.text(project.src / f"{members[0]}.c", source)
            for member in members[1:]:
                atomic_files.remove(project.src / f"{member}.c")
            absorbed.update((member, ()) for member in members[1:])
            firsts.append(members[0])
            touched += [project.src / f"{m}.c" for m in members]
            lines.append(f"merge {group.name} {members[0]}..{members[-1]}: proven")
        touched += _absorb_rows(project, starts)
        # One membership edit for the pass: absorbed members leave with their rows, refused runs become cuts.
        layout_map.edit_members(project, absorbed, proven=firsts)
        touched += buildfiles.write(project_config.load(project.root), host)
        merged = sum(passed for passed in proven)
        land._commit(
            project,
            host,
            sorted(set(touched)),
            f"Merge units: {merged} proven runs, {len(found) - merged} byte mismatches",
        )
    return lines


def run(project: Project, host: Host) -> list[str]:
    from unbake import steps
    from unbake.work.attempts import RetryScope, command_ledger

    with (
        command_ledger(project),
        RetryScope(
            project, "merge", "merge-units", {}, steps.operation_dependencies(project, host, "merge-units")
        ) as scope,
    ):
        result = _run(project, host)
        scope.value = {"receipts": result}
        return result
