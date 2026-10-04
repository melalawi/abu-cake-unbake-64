"""Fold one draft against the shared headers: the folded source, the header texts it needs, the split edits.

A draft sees build/work/FUNC/include/ before include/. Its group header is copied there before folding,
so fold edits a private copy; tidy leaves those copies in the work dir and land publishes them.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from pathlib import Path

from unbake.config import Held, Host, Project, draft_view
from unbake.decomp import checks, gbi
from unbake.fold import declarations, notes
from unbake.layout import map as layout_map
from unbake.layout import split
from unbake.layout.split import Edit


@dataclass(frozen=True)
class Folded:
    function: str
    source: str
    headers: dict[str, str]
    split_edits: tuple[Edit, ...]
    notes: tuple[str, ...] = field(default=())


def view(project: Project, function: str) -> Project:
    """The draft view, with the function's group header copied into the work include dir once."""
    drafted = draft_view(project, function)
    owner = layout_map.load(project).owners.get(function)
    if owner is None:
        raise Held("layout", f"layout.member.{function}: function has no group in layout.toml")
    work_root = drafted.work_include[0]
    shared = project.include[-1] / owner.header
    private = work_root / owner.header
    if shared.is_file() and not private.exists():
        private.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(shared, private)
    return drafted


def relative_header(project: Project, path: Path) -> str | None:
    for root in project.include:
        if path.is_relative_to(root):
            return path.relative_to(root).as_posix()
    return None


def fold(project: Project, host: Host, function: str, text: str) -> Folded:
    """Lower GBI, fold shared types and plan the publication edits, without writing project files."""
    drafted = view(project, function)
    versions = split.holding_versions(project, function)
    lowered = gbi.prepare(drafted, text, gbi.microcode(drafted))
    source = lowered.source
    if "gbi" in lowered.headers:
        source = gbi.install(drafted) + source
    if "abi" in lowered.headers:
        source = gbi.install_audio(drafted) + source
    with notes.collect() as learned:
        edits = declarations.folded_edits(drafted, host, function, source, versions)
    headers: dict[str, str] = {}
    split_edits = []
    folded = None
    for edit in edits:
        if edit.path == drafted.src / f"{function}.c":
            folded = edit.after
            continue
        name = relative_header(drafted, edit.path)
        if name is not None:
            headers[name] = edit.after
        else:
            split_edits.append(edit)
    if folded is None:
        raise Held("fold", f"fold.source: {function}: fold produced no source")
    blockers = [finding for finding in checks.run(folded) if finding.fakematch is None]
    if blockers:
        raise Held("fold", "fold.source_rules: " + "; ".join(checks.message(finding) for finding in blockers))
    return Folded(function, folded, headers, tuple(split_edits), tuple(learned))


def private_headers(project: Project, function: str) -> dict[str, str]:
    """Every header in the draft's work include dir, by relative name."""
    root = project.work / function / "include"
    if not root.is_dir():
        return {}
    return {path.relative_to(root).as_posix(): path.read_text() for path in sorted(root.rglob("*.h"))}
