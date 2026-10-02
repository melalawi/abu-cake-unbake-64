"""Preflight shared layouts and prepare final source declarations."""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import replace
from pathlib import Path

from unbake.decomp import drafts, needs
from unbake.layout import split, structs, structs_identity
from unbake.layout.split import Edit
from unbake.layout.structs_fold import fold, scalar_edits
from unbake.layout.structs_parser import Parser
from unbake.match import reporting, source_views, type_rewrite
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
        combined = "\n".join(headers.values())
        parsed = Parser(combined).parse()
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


def folded_edits(
    project: Project, policy: Policy, function: str, text: str, versions: tuple[str, ...], *, prove_headers: bool = True
) -> list[Edit]:
    """Plan aggregate promotion and source removal as one publication unit."""
    parsers = source_views.parsers(project, policy, text, versions)
    text, tag_only = _layout_names(project, policy, function, text, parsers, versions)
    parsers = source_views.parsers(project, policy, text, versions)
    records = [record for parser in parsers for record in parser.parse()]
    records = [replace(record, aliases=()) if record.name in tag_only else record for record in records]
    destination = project.include[0] / "shared" / f"{function.lower()}.h"
    headers = fold(records, project, destination=destination, prove_headers=prove_headers)
    final = final_source(project, text, parsers, headers)
    return [*headers, *match_edits(project, function, final, versions)]


def _layout_names(
    project: Project, policy: Policy, function: str, text: str, parsers: list[Parser], versions: tuple[str, ...]
) -> tuple[str, set[str]]:
    """Rewrite active type tokens using complete layout evidence, before merging fields."""
    paths = sorted({path for root in project.include for path in root.rglob("*.h")})
    contents = {path: path.read_text() for path in paths}
    existing = Parser("\n".join(contents.values())).parse()
    tag_only = {record.name for record in existing if record.name not in record.aliases}
    resolved_tags: set[str] = set()
    sdk: set[str] = set()
    cursor = 0
    for path, content in contents.items():
        if path.name == "n64sdk.h":
            sdk.update(
                name
                for record in existing
                if cursor <= record.start < cursor + len(content)
                for name in (record.name, *record.aliases)
            )
        cursor += len(content) + 1
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

    from unbake.typemap.declarations import headers

    for index, parser in enumerate(parsers):
        # Validate canonical scalar names before rewriting any aggregate alias.
        scalar_edits(project, parser)
        records = parser.parse()
        resolution = structs_identity.resolve([record for record in records if record.name not in sdk], existing)
        resolved_tags.update(target for target, _ in resolution.values() if target in tag_only)
        if not resolution:
            continue
        planned = type_rewrite.edits(parser, headers(project, policy, versions[index]), resolution, tag_only)
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
