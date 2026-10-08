"""Plan the published consumers of newly shared, formerly file-local layouts."""

from __future__ import annotations

import re
from collections import ChainMap
from collections.abc import Iterator
from dataclasses import replace
from functools import partial
from pathlib import Path

from unbake import scratch
from unbake.cdecl import LayoutParser
from unbake.config import Held, Host, Project
from unbake.fold import rewrite_view, source_views, type_rewrite
from unbake.layout import split
from unbake.layout.header_context import Headers
from unbake.layout.split import Edit
from unbake.layout.structs import Layout
from unbake.layout.structs_identity import identity
from unbake.process import named as cause_named


def _names(records: list[Layout]) -> dict[str, Layout]:
    return {name: record for record in records for name in (record.name, *record.aliases)}


def _units(
    project: Project, host: Host, headers: Headers, function: str, names: set[str]
) -> Iterator[tuple[Path, str, tuple[str, ...], list[LayoutParser]]]:
    if not names:
        return
    owners = {version: split.owners_by_alias(project, version) for version in project.versions}
    for source in sorted(project.src.glob("*.c")):
        if source.stem == function:
            continue
        text = source.read_text()
        tags = set(re.findall(r"\b(?:struct|union)\s+(\w+)\s*\{", text))
        if not tags & names:
            continue
        versions = tuple(
            version
            for version in split.code_versions(project, source.stem, owners)
            if any(row.kind == "c" for row in owners[version].get(source.stem, ()))
        )
        if versions:
            yield source, text, versions, source_views.parsers(project, host, text, versions, source.stem, headers)


def reserved(project: Project, host: Host, headers: Headers, function: str, records: list[Layout]) -> set[str]:
    """A different published layout reserves its tag before choosing a promotion name."""
    requested = _names(records)
    occupied: set[str] = set()
    for _, _, _, parsers in _units(project, host, headers, function, set(requested)):
        for parser in parsers:
            local = [parser.layout(item) for item in parser.aggregates if item.name]
            names = ChainMap(_names(local), headers.index.names)
            for record in local:
                target = requested.get(record.name)
                if target is not None and identity(record, names) != identity(
                    target, ChainMap(requested, headers.index.names)
                ):
                    occupied.add(record.name)
    return occupied


def plan(project: Project, host: Host, function: str, headers: dict[str, str]) -> tuple[Edit, ...]:
    """Reconstruct consumer edits from staged headers, including a previous tidy's copies."""
    from unbake.fold.declarations import final_source

    public = replace(project, work_include=())
    before = Headers.read(public)
    changed = {public.include[-1] / name: text for name, text in headers.items()}
    after = Headers({**before.texts, **changed}, root=project.root)
    promoted = [record for record in after.records if record.name not in before.index.names]
    names = set(_names(promoted))
    # A conflicting tag is qualified by the publishing owner. Its compatible
    # consumers still spell the original tag, including after tidy removed it.
    names.update(record.name.split(f"_{function}", 1)[0] for record in promoted)
    candidates: dict[str, Layout] = {}
    for record in promoted:
        candidates.setdefault(identity(record, after.index.names), record)
    edits: list[Edit] = []
    for source, text, versions, parsers in _units(public, host, before, function, names):
        replacements: dict[tuple[int, int], str] = {}
        selected: set[str] = set()
        with scratch.temporary(host, project, "fold", prefix="shared-consumers-") as temporary:
            roots = source_views.header_includes(public, before, Path(temporary))
            context_project = replace(public, work_include=roots)
            for index, parser in enumerate(parsers):
                records = [parser.layout(item) for item in parser.aggregates if item.name]
                local = ChainMap(_names(records), before.index.names)
                resolution = {}
                for record in records:
                    provider = candidates.get(identity(record, local))
                    if provider is None:
                        if record.name in _names(promoted):
                            raise Held(
                                cause_named(
                                    "fold.layout",
                                    (
                                        f"fold.layout: {function}.c and {source.name}: conflicting published "
                                        f"layout for {record.kind} {record.name}"
                                    ),
                                    owner="fold.shared_consumers",
                                    stage="fold",
                                )
                            )
                        continue
                    selected.add(provider.name)
                    resolution.update(dict.fromkeys((record.name, *record.aliases), (provider.name, provider)))
                if not resolution:
                    continue
                planned = type_rewrite.edits(
                    parser,
                    partial(
                        source_views.typed_context,
                        public,
                        host,
                        before,
                        versions[index],
                        source.stem,
                        context_project=context_project,
                    ),
                    resolution,
                    after.tag_only,
                    cache_root=project.cache,
                    preprocess=partial(
                        rewrite_view.prepare,
                        context_project,
                        host,
                        text,
                        versions[index],
                        source,
                        contents=source_views.authored_contents(public, before, context_project),
                    ),
                    source_path=source,
                    source_text=text,
                )
                for span, target in planned.items():
                    if span in replacements and replacements[span] != target:
                        raise Held(
                            cause_named(
                                "fold.layout",
                                f"fold.layout: {function}.c and {source.name}: version-dependent rename",
                                owner="fold.shared_consumers",
                                stage="fold",
                            )
                        )
                    replacements[span] = target
        if not selected:
            continue
        rewritten = text
        for (start, end), target in sorted(replacements.items(), reverse=True):
            rewritten = rewritten[:start] + target + rewritten[end:]
        parsers = source_views.parsers(public, host, rewritten, versions, source.stem, before)
        rewritten = final_source(public, rewritten, parsers, [], after, public.include[-1], selected=selected)
        if rewritten != text:
            edits.append(Edit(source, text, rewritten, versions))
    return tuple(edits)
