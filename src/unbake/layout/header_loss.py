"""Refuse lost published declaration dependencies before any header writer installs bytes."""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any, cast

from unbake import cache, inputs, pool, tui
from unbake import cache as retention
from unbake.cache import memo
from unbake.cdecl import declaration_source, declarations
from unbake.config import Held, Host, Project
from unbake.process import named as cause_named
from unbake.project.headers import Graph, HeaderCheck, recipe, retention_recipe
from unbake.project.headers import project as projection

_TAG = re.compile(r"\b(struct|union|enum)\s+(\w+)\s*\{")
_TAG_USE = re.compile(r"\b(struct|union|enum)\s+(\w+)")


def _words(text: str) -> set[str]:
    from unbake.layout.header_step import uses

    code = declaration_source(text)
    return uses(text) | {f"{kind} {name}" for kind, name in _TAG_USE.findall(code)}


def _source(text: str) -> tuple[frozenset[str], frozenset[str]]:
    from unbake.layout import redeclarations

    def compute() -> tuple[frozenset[str], frozenset[str]]:
        provided = set().union(
            *(
                declarations(variant).typedefs
                for start, end in redeclarations.spans(text)
                for variant in redeclarations.variants(text[start:end])
            )
        )
        provided |= {f"{kind} {name}" for kind, name in _TAG.findall(declaration_source(text))}
        return frozenset(provided), frozenset(_words(text) - provided)

    return memo("header-loss.source", text, compute, size=retention.memory_size, copy_out=retention.clone)


@pool.cpu
def _source_job(
    shared: tuple[Graph, Graph, set[str], dict[Path, set[str]], dict[str, set[str]]], rows: list[tuple[Path, str, str]]
) -> list[str]:
    before, after, kept, lost, dependencies = shared
    refusals = []
    for source, old_text, text in rows:
        provided, words = _source(text)
        wanted = set(words)
        pending = list(wanted)
        while pending:
            name = pending.pop()
            added = dependencies.get(name, set()) - wanted - provided
            wanted.update(added)
            pending.extend(added)
        for path, names in lost.items():
            missing = names & wanted
            if missing:
                refusals.append(f"{path}: would remove {', '.join(sorted(missing))} used by published C ({source})")
        unreachable = (before.included(source, old_text) - after.included(source, text)) & wanted & kept
        if unreachable:
            refusals.append(f"{source}: would lose reachable declarations {', '.join(sorted(unreachable))}")
    return refusals


def _header(text: str, reader: str) -> tuple[frozenset[str], dict[str, set[str]]]:
    row = projection(text, reader)
    if row.parse_error:
        raise Held(
            cause_named(
                "headers.projection",
                f"headers.projection: {row.parse_error}",
                owner="layout.header_loss",
                stage="headers",
            )
        )
    dependencies: dict[str, set[str]] = {}
    for statement in row.statements:
        part = projection(statement, reader)
        if part.parse_error:
            raise Held(
                cause_named(
                    "headers.projection",
                    f"headers.projection: {part.parse_error}",
                    owner="layout.header_loss",
                    stage="headers",
                )
            )
        for name in part.names:
            dependencies.setdefault(name, set()).update(_words(statement))
    return row.names, dependencies


@pool.cpu
def _header_job(shared: tuple[Path, str], texts: list[str]) -> list[tuple[frozenset[str], dict[str, set[str]]]]:
    root, reader = shared
    store = cache.Cache(root)
    result = []
    for text in texts:

        def complete(text: str = text) -> Any:
            return projection(text, reader)

        def retained(text: str = text) -> tuple[frozenset[str], dict[str, set[str]]]:
            return _header(text, reader)

        store.value("header-projection", cache.key(text, reader, "C", "declarations"), cache.PICKLE, complete)
        result.append(store.value("header-retention", cache.key(text, reader), cache.PICKLE, retained))
    return result


def output_key(outputs: Mapping[Path, bytes | Path], obsolete: Iterable[Path]) -> str:
    return cache.key(
        *(
            part
            for path, data in sorted(outputs.items())
            for part in (str(path), data.read_bytes() if isinstance(data, Path) else data)
        ),
        *(str(path) for path in sorted(obsolete)),
    )


def check(
    project: Project, outputs: Mapping[Path, bytes | Path], *, obsolete: Iterable[Path] = (), policy: Host | None = None
) -> HeaderCheck:
    removed_paths = set(obsolete)
    before = Graph.capture(project)
    after = Graph(before.view.overlay({**dict.fromkeys(removed_paths), **outputs}), before.search)
    changes = {
        p
        for p in set(outputs) | removed_paths
        if p not in before.view.files or p not in after.view.files or before.view.read(p) != after.view.read(p)
    }
    inputs_key = output_key(outputs, removed_paths)
    reader = recipe()
    dependency_set = inputs.snapshot(
        before.view,
        (before.view.logical(p) for p in before.view.files),
        values={"inventory": sorted(before.view.logical(p).name for p in before.view.files)},
        recipes={"graph": reader, "retention": retention_recipe()},
    )
    store = cache.Cache(project.cache)
    content = cache.key(dependency_set.digest, inputs_key)
    reused = store.path("header-check", content).is_file()

    def validate() -> HeaderCheck:
        if not changes:
            return HeaderCheck(0, 0, 0, 0, (), dependency_set, outputs_digest=inputs_key)
        contents = [
            {p: graph.view.read(p).decode() for p in graph.view.files if p.suffix == ".h"} for graph in (before, after)
        ]
        texts = list(dict.fromkeys(text for view in contents for text in view.values()))
        jobs = [texts[start : start + 32] for start in range(0, len(texts), 32)]
        shared = project.cache, reader
        with tui.task("Indexing retained header declarations", len(texts)):
            done = (
                [_header_job(shared, job) for job in jobs]
                if policy is None
                else pool.run(policy, _header_job, jobs, shared)
            )
        projections = dict(zip(texts, (row for batch in done for row in batch), strict=True))
        # The Graph projection is stored by the same worker-side parser and recipe.
        # Parent graph methods retrieve complete projections through Cache.
        for path in changes:
            for graph in (before, after):
                if path.suffix == ".h" and path in graph.view.files:
                    row = graph.projection(path)
                    if any(i.unknown or i.conditional for i in row.includes):
                        raise Held(
                            cause_named(
                                "headers.include_unknown",
                                f"headers.include_unknown: {path}: native dependency proof required",
                                owner="layout.header_loss",
                                stage="headers",
                            )
                        )
        kept = set().union(*(projections[text][0] for text in contents[1].values()))
        lost = {p: set(projections[text][0]) - kept for p, text in contents[0].items() if p in changes}
        dependencies: dict[str, set[str]] = {}
        for text in contents[0].values():
            for name, words in projections[text][1].items():
                dependencies.setdefault(name, set()).update(words)
        sources = {p for graph in (before, after) for p in graph.view.files if p.suffix == ".c"}
        affected = after.affected(before, changes, sources)
        rows = [
            (p, before.view.read(p).decode() if p in before.view.files else "", after.view.read(p).decode())
            for p in affected
            if p in after.view.files
        ]
        shared_sources = before, after, kept, lost, dependencies
        jobs_sources = [rows[start : start + 32] for start in range(0, len(rows), 32)]
        with tui.task("Checking retained source dependencies", len(rows)):
            checked = (
                [_source_job(shared_sources, job) for job in jobs_sources]
                if policy is None
                else pool.run(policy, _source_job, jobs_sources, shared_sources)
            )
        refusals = [reason for batch in checked for reason in batch]
        if refusals:
            raise Held(
                cause_named(
                    "layout.header_loss.validate",
                    "headers.merge_only: " + "; ".join(refusals),
                    owner="layout.header_loss",
                    stage="headers",
                )
            )
        return HeaderCheck(len(texts), 0, len(rows), 0, affected, dependency_set, outputs_digest=inputs_key)

    result = store.value("header-check", content, cache.PICKLE, validate)
    if reused:
        from dataclasses import replace

        result = replace(
            result,
            headers_checked=0,
            headers_reused=result.headers_checked,
            closures_checked=0,
            closures_reused=result.closures_checked,
        )
    return cast(HeaderCheck, result)
