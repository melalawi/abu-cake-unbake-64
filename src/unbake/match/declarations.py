"""Preflight shared layouts and prepare final source declarations."""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import replace
from pathlib import Path

from unbake.decomp import drafts, needs
from unbake.layout import entries, split, structs
from unbake.layout.header_context import context
from unbake.layout.split import Edit
from unbake.layout.structs_fold import fold, scalar_edits
from unbake.layout.structs_parser import Parser
from unbake.match import source_views
from unbake.project.config import Policy, Project


def preflight(project: Project, policy: Policy, pending: list[needs.Need]) -> list[Edit]:
    """Validate shared layout needs without writes, copies, generations, or builds.

    Call after deriving trial needs and before retaining or submitting a trial.
    Missing aggregates produce proposed shared-header edits; conflicts raise Held.
    """
    return structs.resolve([need for need in pending if isinstance(need, needs.LayoutNeed)], project, policy)


def final_source(project: Project, text: str, parsers: list[Parser], edits: list[Edit]) -> str:
    """Move local aggregate definitions to their shared homes and remove draft markers."""
    records = [record for parser in parsers for record in parser.parse()]
    includes: set[str] = set()
    replacements: list[tuple[int, int, str]] = []
    for parser in parsers:
        selected, removed = scalar_edits(project, parser)
        includes.update(selected)
        replacements.extend(removed)
    if records:
        headers = {path: path.read_text() for root in project.include for path in Path(root).rglob("*.h")}
        headers.update({edit.path: edit.after for edit in edits})
        destinations: dict[str, Path] = {}
        headers, _, parsed = context(headers, root=project.root)
        cursor = 0
        for path, content in headers.items():
            for record in parsed:
                if cursor <= record.start < cursor + len(content):
                    for name in (record.name, *record.aliases):
                        destinations[name] = path
            cursor += len(content) + 1
        spans: list[tuple[int, int]] = []
        for record in records:
            destination = destinations[record.name]
            include = next(
                destination.relative_to(root).as_posix() for root in project.include if destination.is_relative_to(root)
            )
            includes.add(include)
        names = {name for record in records for name in (record.name, *record.aliases)}
        for declaration in (item for parser in parsers for item in parser.declarations):
            base = declaration.base
            name = base if isinstance(base, str) else base.name
            if name in names and not declaration.operations:
                spans.append((declaration.start, declaration.end))
        replacements.extend((start, end, "") for start, end in set(spans))
    for start, end, replacement in sorted(set(replacements), reverse=True):
        text = text[:start] + replacement + text[end:]
    for include in sorted(includes):
        if not re.search(rf'^\s*#\s*include\s*[<"]{re.escape(include)}[>"]', text, re.M):
            text = f'#include "{include}"\n' + text
    return re.sub(r"^[ \t]*/\*\s*NON_MATCHING:\s*draft\b[^\n]*\*/[ \t]*\n?", "", text, flags=re.M)


def folded_edits(project: Project, policy: Policy, function: str, text: str, versions: tuple[str, ...]) -> list[Edit]:
    """Plan aggregate promotion and source removal as one publication unit."""
    parsers = source_views.parsers(project, policy, text, versions)
    records = [record for parser in parsers for record in parser.parse()]
    destination = project.include[0] / "shared" / f"{function.lower()}.h"
    headers = fold(records, project, destination=destination)
    final = final_source(project, text, parsers, headers)
    edits = match_edits(project, function, final, versions)
    for version in versions:
        group = entries.owners(project, policy, project.src / f"{function}.c", version, text=text)
        if len(group) < 2:
            continue
        configured = project.version(version)
        _, lines, segments = split.layout(configured.split)
        paths = {row.path for row in group[1:]}
        removed = [lines[row.line] for segment in segments for row in segment.rows if row.path in paths]
        edits = [
            replace(edit, after=_remove_rows(edit.after, removed)) if edit.path == configured.split else edit
            for edit in edits
        ]
    return [*headers, *edits]


def _remove_rows(text: str, removed: list[str]) -> str:
    for line in removed:
        text = text.replace(line, "", 1)
    return text


def match_edits(project: Project, function: str, text: str, versions: Iterable[str]) -> list[Edit]:
    """Publish an assembly-backed source, including committed unguarded drafts."""
    path = project.src / f"{function}.c"
    versions = tuple(versions)
    assembly = []
    for version in versions:
        _, _, segments = split.layout(project.version(version).split)
        rows = [
            row
            for segment in segments
            for row in segment.rows
            if row.kind in ("asm", "c") and Path(row.path).name == function
        ]
        if len(rows) == 1 and rows[0].kind == "c":
            continue
        assembly.append(version)
    if not assembly:
        return [Edit(path, path.read_text() if path.exists() else "", text, versions)]
    if not path.exists() or drafts.is_partial(path.read_text()):
        edits = drafts.match_edits(project, function, text, assembly)
        edits[0] = replace(edits[0], versions=versions)
        return edits
    # Derive the same publication edits without treating an assembly-backed draft
    # as an already matched source. The selected split rows still require asm.
    unpublished = replace(project, src=project.src / ".match-unpublished")
    edits = drafts.match_edits(unpublished, function, text, assembly)
    edits[0] = replace(edits[0], path=path, before=path.read_text(), versions=versions)
    return edits
