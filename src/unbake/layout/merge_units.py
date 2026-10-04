"""The `merge-units` step: try one merged C unit for each new run of adjacent matched members in a group.

A run is two or more consecutive members of one layout.toml group that are all published (src/NAME.c), whose
rows are contiguous in every holding version, held by the same versions, built by the same compiler and not
separated by a recorded `split` cut. The merged source is src/<first>.c: the union of the members' include
lines, then their bodies in address order. It is proved like a land: in every holding version the strict
`n64link place` link of the merged unit must equal the ROM bytes of the whole run.
- Pass: write the merged source, remove the others, absorb their split rows into the first row, mark the group
  `evidence = "proven"`, commit "Merge units into <group> run".
- Fail: record every member after the first as a split cut, so the run is never tried again until someone
  removes the cut (the evidence changed).
"""

from __future__ import annotations

import re
import tempfile
from dataclasses import replace
from pathlib import Path

from unbake import atomic as atomic_files
from unbake import cache, runner, steps
from unbake.config import Held, Host, Project
from unbake.layout import map as layout_map
from unbake.layout import split
from unbake.work import compare

_INCLUDE = re.compile(r"^[ \t]*#[ \t]*include[^\n]*\n?", re.M)


def input_key(project: Project) -> str:
    parts: list[str | bytes | Path] = [steps.tool_fingerprint(), project.root / "layout.toml"]
    parts.extend(sorted(project.src.glob("*.c")))
    return cache.key(*parts)


def runs(project: Project) -> list[tuple[layout_map.Group, tuple[str, ...]]]:
    """Mergeable runs, in layout order."""
    found = []
    for group in layout_map.load(project).groups:
        current: list[str] = []
        for member in (*group.members, None):
            mergeable = (
                member is not None
                and (project.src / f"{member}.c").is_file()
                and not (current and member in group.split)
                and (not current or _joins(project, current[-1], member))
            )
            if mergeable:
                current.append(member)  # type: ignore[arg-type]
                continue
            if len(current) >= 2:
                found.append((group, tuple(current)))
            current = [member] if member is not None and (project.src / f"{member}.c").is_file() else []
    return found


def _joins(project: Project, left: str, right: str) -> bool:
    versions = split.holding_versions(project, left)
    if versions != split.holding_versions(project, right):
        return False
    if project.compiler_reference(left) != project.compiler_reference(right):
        return False
    return all(compare.row_of(project, left, v).end == compare.row_of(project, right, v).start for v in versions)


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


def prove(project: Project, host: Host, members: tuple[str, ...], source: str) -> bool:
    first = members[0]
    with tempfile.TemporaryDirectory(prefix="merge-") as temporary:
        work = Path(temporary)
        file = work / f"{first}.c"
        atomic_files.text(file, source)
        for version in split.holding_versions(project, first):
            head = compare.row_of(project, first, version)
            tail = compare.row_of(project, members[-1], version)
            row = replace(head, end=tail.end)
            try:
                obj = runner.compile_unit(project, host, file, version, unit=first)
                placed = work / f"{version}.placed.o"
                runner.place(project, host, obj, version, row, placed, score=False)
                linked = runner.link(project, host, placed, version, row, work)
            except Held:
                return False
            if linked != split.words(project, row):
                return False
    return True


def _absorb_rows(project: Project, members: tuple[str, ...]) -> list[Path]:
    """Remove the split rows of every member after the first, in each holding version."""
    changed = []
    for version in split.holding_versions(project, members[0]):
        path = project.version(version).split
        text, lines, segments = split.layout(path)
        lines = list(lines)
        starts = {compare.row_of(project, member, version).start for member in members[1:]}
        drop = {row.line for segment in segments for row in segment.rows if row.start in starts and row.kind == "c"}
        after = "".join(line for index, line in enumerate(lines) if index not in drop)
        if after != text:
            atomic_files.text(path, after)
            changed.append(path)
    return changed


def _group_update(project: Project, name: str, segment: str, **changes: object) -> Path:
    value = layout_map.load(project)
    groups = tuple(
        replace(group, **changes) if (group.name, group.segment) == (name, segment) else group  # type: ignore[arg-type]
        for group in value.groups
    )
    path = project.root / "layout.toml"
    atomic_files.write(path, layout_map.encoded(replace(value, groups=groups)))
    return path


def _commit(project: Project, host: Host, paths: list[Path], message: str) -> None:
    from unbake import land

    land._git(project, "add", "-A", "--", *(str(p.relative_to(project.root)) for p in paths))
    land._git(
        project,
        "-c",
        f"user.name={host.publish_author_name}",
        "-c",
        f"user.email={host.publish_author_email}",
        "commit",
        "-q",
        "-m",
        message,
        "--author",
        f"{host.publish_author_name} <{host.publish_author_email}>",
    )


def run(project: Project, host: Host) -> list[str]:
    """Try each run once; return one line per attempt."""
    from unbake import buildfiles
    from unbake import config as project_config

    lines = []
    for group, members in runs(project):
        source = merged_source(project, members)
        if not prove(project, host, members, source):
            cuts = tuple(dict.fromkeys((*group.split, *members[1:])))
            path = _group_update(project, group.name, group.segment, split=cuts)
            _commit(project, host, [path], f"Record unit split in {group.name}")
            lines.append(f"merge {group.name} {members[0]}..{members[-1]}: refused; recorded as split")
            continue
        backup = {
            path: path.read_bytes()
            for path in [project.root / "layout.toml", *(project.src / f"{m}.c" for m in members)]
        }
        backup.update({project.version(v).split: project.version(v).split.read_bytes() for v in project.versions})
        try:
            atomic_files.text(project.src / f"{members[0]}.c", source)
            for member in members[1:]:
                (project.src / f"{member}.c").unlink()
            touched = [
                project.src / f"{members[0]}.c",
                *(project.src / f"{m}.c" for m in members[1:]),
                *_absorb_rows(project, members),
                _group_update(project, group.name, group.segment, evidence="proven"),
            ]
            reloaded = project_config.load(project.root)
            touched += buildfiles.write(reloaded, host)
            _commit(project, host, touched, f"Merge units into {group.name} run")
        except BaseException:
            for path, content in backup.items():
                atomic_files.write(path, content)
            raise
        lines.append(f"merge {group.name} {members[0]}..{members[-1]}: proven and committed")
        project = project_config.load(project.root)
    return lines
