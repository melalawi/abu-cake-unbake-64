"""Preflight shared layouts and prepare final source declarations."""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, replace
from pathlib import Path

from unbake.decomp import drafts, needs
from unbake.layout import entries, split, structs
from unbake.layout.header_context import Headers
from unbake.layout.split import Edit
from unbake.layout.structs_fold import fold, scalar_edits
from unbake.layout.structs_parser import Parser
from unbake.match import reporting, source_views, type_rewrite
from unbake.match.common import held
from unbake.project.config import Policy, Project


def preflight(project: Project, policy: Policy, pending: list[needs.Need]) -> list[Edit]:
    """Validate shared layout needs without writes, copies, generations, or builds.

    Call after deriving trial needs and before retaining or submitting a trial.
    Missing aggregates produce proposed shared-header edits; conflicts raise Held.
    """
    return structs.resolve([need for need in pending if isinstance(need, needs.LayoutNeed)], project, policy)


def final_source(
    project: Project, text: str, parsers: list[Parser], edits: list[Edit], headers: Headers, destination: Path
) -> str:
    """Move local aggregate definitions to their shared homes and remove draft markers."""
    records = [record for parser in parsers for record in _records(parser)]
    includes: set[str] = set()
    replacements: list[tuple[int, int, str]] = []
    for parser in parsers:
        selected, removed = scalar_edits(project, parser, headers)
        includes.update(selected)
        replacements.extend(removed)
    if records:
        added = {edit.path for edit in edits if edit.path not in headers.texts}
        spans: list[tuple[int, int]] = []
        for record in records:
            home = headers.homes.get(record.name) or (destination if destination in added else None)
            if home is None:
                held(f"{record.name}: shared declaration home missing after fold")
            includes.add(
                next(home.relative_to(root).as_posix() for root in project.include if home.is_relative_to(root))
            )
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


def _records(parser: Parser) -> list[structs.Layout]:
    """Layouts of an already parsed source view, without parsing it again."""
    return [parser.layout(item) for item in parser.aggregates if item.name]


@dataclass(frozen=True)
class Folded:
    """One source after its local layouts moved into shared headers."""

    function: str
    source: str
    headers: list[Edit]
    removed_rows: dict[str, tuple[str, ...]]


def fold_source(
    project: Project,
    policy: Policy,
    headers: Headers,
    function: str,
    text: str,
    versions: tuple[str, ...],
    *,
    prove_headers: bool = True,
) -> Folded:
    """Plan aggregate promotion against a shared header context; the context is not changed."""
    parsers = source_views.parsers(project, policy, text, versions, headers)
    text, tag_only = _layout_names(project, policy, function, text, parsers, versions, headers)
    parsers = source_views.parsers(project, policy, text, versions, headers)
    records = [record for parser in parsers for record in _records(parser)]
    records = [replace(record, aliases=()) if record.name in tag_only else record for record in records]
    destination = project.include[0] / "shared" / f"{function.lower()}.h"
    edits = fold(records, project, destination=destination, prove_headers=prove_headers, context=headers)
    final = final_source(project, text, parsers, edits, headers, destination)
    removed: dict[str, tuple[str, ...]] = {}
    for version in versions:
        group = entries.owners(project, policy, project.src / f"{function}.c", version, text=text)
        if len(group) < 2:
            continue
        _, lines, segments = split.layout(project.version(version).split)
        paths = {row.path for row in group[1:]}
        removed[version] = tuple(lines[row.line] for segment in segments for row in segment.rows if row.path in paths)
    return Folded(function, final, edits, removed)


def folded_edits(
    project: Project, policy: Policy, function: str, text: str, versions: tuple[str, ...], *, prove_headers: bool = True
) -> list[Edit]:
    """Plan aggregate promotion and source removal as one publication unit."""
    folded = fold_source(project, policy, Headers.read(project), function, text, versions, prove_headers=prove_headers)
    edits = match_edits(project, function, folded.source, versions)
    edits = [
        replace(edit, after=_remove_rows(edit.after, folded.removed_rows[version]))
        if (version := next((v for v in versions if project.version(v).split == edit.path), None))
        and version in folded.removed_rows
        else edit
        for edit in edits
    ]
    return [*folded.headers, *edits]


def _remove_rows(text: str, removed: Iterable[str]) -> str:
    for line in removed:
        text = text.replace(line, "", 1)
    return text


def _layout_names(
    project: Project,
    policy: Policy,
    function: str,
    text: str,
    parsers: list[Parser],
    versions: tuple[str, ...],
    headers: Headers,
) -> tuple[str, set[str]]:
    """Rewrite active type tokens using complete layout evidence, before merging fields."""
    tag_only = headers.tag_only
    resolved_tags: set[str] = set()
    sdk = headers.sdk
    replacements: dict[tuple[int, int], str] = {}
    redundant: dict[tuple[int, int], str] = {}
    renames: set[tuple[str, str]] = set()
    member_renames: set[tuple[str, str]] = set()

    def renamed_members(old: tuple[structs.Field, ...], new: tuple[structs.Field, ...], left: str, right: str) -> None:
        for field, target in zip(old, new, strict=True):
            before, after = f"{left}.{field.name}", f"{right}.{target.name}"
            if field.name != target.name:
                member_renames.add((before, after))
            renamed_members(field.fields, target.fields, before, after)

    from unbake.typemap.declarations import headers as typed_headers

    for index, parser in enumerate(parsers):
        # Validate canonical scalar names before rewriting any aggregate alias.
        scalar_edits(project, parser, headers)
        records = _records(parser)
        resolution = headers.index.resolve([record for record in records if record.name not in sdk])
        resolved_tags.update(target for target, _ in resolution.values() if target in tag_only)
        if not resolution:
            continue
        planned = type_rewrite.edits(parser, typed_headers(project, policy, versions[index]), resolution, tag_only)
        for span, target in planned.items():
            if span in replacements and replacements[span] != target:
                structs.held(function, "version-dependent layout rename at the same source token")
            replacements[span] = target
        defined: set[str] = set()
        for record in records:
            if record.name not in resolution:
                continue
            target, _ = resolution[record.name]
            if target in defined:
                declaration = next(
                    (item for item in parser.declarations if getattr(item.base, "start", None) == record.start), None
                )
                if declaration is not None:
                    redundant[(declaration.start, declaration.end)] = (
                        f"typedef {record.kind} {target} {target};" if record.aliases else ""
                    )
            defined.add(target)
        for name, (target, _) in resolution.items():
            if name != target:
                renames.add((name, target))
        for record in records:
            if record.name in resolution:
                target, evidence = resolution[record.name]
                renamed_members(record.fields, evidence.fields, record.name, target)
    replacements = {
        span: target
        for span, target in replacements.items()
        if not any(start <= span[0] and span[1] <= end for start, end in redundant)
    }
    replacements.update(redundant)
    for (start, end), target in sorted(replacements.items(), reverse=True):
        text = text[:start] + target + text[end:]
    for name, target in sorted(renames):
        reporting.learn(f"OK(types): {function}: rename {name} -> {target} (shared layout)")
    for name, target in sorted(member_renames):
        reporting.learn(f"OK(types): {function}: rename {name} -> {target} (shared member layout)")
    return text, resolved_tags


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
