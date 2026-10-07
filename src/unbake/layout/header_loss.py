"""Refuse lost published declaration dependencies before any header writer installs bytes."""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

from unbake import pool, tui
from unbake.cache import memo
from unbake.cdecl import declaration_source, declarations
from unbake.config import Held, Host, Project

_DEFINE = re.compile(r"^[ \t]*#[ \t]*define[ \t]+(\w+)", re.M)
_TAG = re.compile(r"\b(struct|union|enum)\s+(\w+)\s*\{")
_INCLUDE = re.compile(r'^[ \t]*#[ \t]*include[< \t"]+([^>"\n]+)[>"]', re.M)
_TAG_USE = re.compile(r"\b(struct|union|enum)\s+(\w+)")


def declared(text: str) -> set[str]:
    """Ordinary identifiers and complete tags occupy separate C namespaces."""
    row = declarations(text)
    return (
        row.typedefs
        | row.declared
        | set(_DEFINE.findall(text))
        | {f"{kind} {name}" for kind, name in _TAG.findall(declaration_source(text))}
    )


def _words(text: str) -> set[str]:
    from unbake.layout.header_step import uses

    code = declaration_source(text)
    return uses(text) | {f"{kind} {name}" for kind, name in _TAG_USE.findall(code)}


def _names(text: str) -> frozenset[str]:
    return memo("header-loss.names", text, lambda: frozenset(declared(text)), keep=32768)


def _header(text: str) -> tuple[frozenset[str], dict[str, set[str]], tuple[str, ...]]:
    from unbake.typemap.declaration_evidence import units

    def compute() -> tuple[frozenset[str], dict[str, set[str]], tuple[str, ...]]:
        dependencies: dict[str, set[str]] = {}
        for unit in units({Path("header.h"): text}):
            for name in _names(unit.text):
                dependencies.setdefault(name, set()).update(_words(unit.text))
        return _names(text), dependencies, tuple(_INCLUDE.findall(text))

    return memo("header-loss.header", text, compute, keep=32768)


@pool.cpu
def _header_job(texts: list[str]) -> list[tuple[frozenset[str], dict[str, set[str]], tuple[str, ...]]]:
    return [_header(text) for text in texts]


@dataclass
class View:
    roots: tuple[Path, ...]
    names: dict[Path, frozenset[str]]
    includes: dict[Path, tuple[str, ...]]
    closures: dict[Path, frozenset[str]] = field(default_factory=dict)
    edges: dict[tuple[Path, str], Path | None] = field(default_factory=dict)

    def resolve(self, parent: Path, name: str) -> Path | None:
        key = parent.parent, name
        if key not in self.edges:
            self.edges[key] = next(
                (path for root in (parent.parent, *self.roots) if (path := (root / name).resolve()) in self.names), None
            )
        return self.edges[key]

    def closure(self, path: Path) -> frozenset[str]:
        if path not in self.closures:
            seen = set()
            pending = [path]
            names: set[str] = set()
            while pending:
                header = pending.pop()
                if header in seen:
                    continue
                seen.add(header)
                names.update(self.names[header])
                pending.extend(
                    target for name in self.includes[header] if (target := self.resolve(header, name)) is not None
                )
            self.closures[path] = frozenset(names)
        return self.closures[path]

    def included(self, source: Path, text: str) -> set[str]:
        return set().union(
            *(
                self.closure(target)
                for name in _INCLUDE.findall(text)
                if (target := self.resolve(source, name)) is not None
            )
        )


def _included(project: Project, source: Path, text: str, contents: Mapping[Path, str]) -> set[str]:
    parts = {path: _header(body) for path, body in contents.items()}
    view = View(
        project.include, {path: row[0] for path, row in parts.items()}, {path: row[2] for path, row in parts.items()}
    )
    return view.included(source, text)


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

    return memo("header-loss.source", text, compute, keep=32768)


@pool.cpu
def _source_job(
    shared: tuple[View, View, set[str], dict[Path, set[str]], dict[str, set[str]]], rows: list[tuple[Path, str, str]]
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


def check(
    project: Project,
    outputs: Mapping[Path, bytes | Path],
    *,
    obsolete: Iterable[Path] = (),
    policy: Host | None = None,
) -> None:
    """Check the complete proposed tree, including unchanged headers and rewritten sources.

    Follow old declaration dependencies as well as body uses: losing a field's
    typedef breaks a published unit even when the body only spells its owner.
    Checking each source also supplies the concrete consumer in every refusal.
    """
    removed_paths = set(obsolete)
    before = {path: path.read_text() for root in project.include for path in root.rglob("*.h")}
    after = {path: text for path, text in before.items() if path not in removed_paths}
    for path, data in outputs.items():
        if path.suffix == ".h":
            after[path] = (data.read_bytes() if isinstance(data, Path) else data).decode()
    changed = {path for path, text in before.items() if after.get(path) != text}
    if not changed:
        return
    texts = list(dict.fromkeys([*before.values(), *after.values()]))
    jobs = [texts[start : start + 32] for start in range(0, len(texts), 32)]
    with tui.task("Indexing retained header declarations", len(texts)):
        done = [_header_job(job) for job in jobs] if policy is None else pool.run(policy, _header_job, jobs)
    projections = dict(zip(texts, (row for batch in done for row in batch), strict=True))

    def view(contents: Mapping[Path, str]) -> View:
        return View(
            project.include,
            {path: projections[text][0] for path, text in contents.items()},
            {path: projections[text][2] for path, text in contents.items()},
        )

    kept = set().union(*(projections[text][0] for text in after.values()))
    removed = {
        path: set(projections[before[path]][0] - projections.get(after.get(path, ""), (frozenset(), {}, ()))[0])
        for path in sorted(changed)
    }
    if not any(removed.values()):
        return
    lost = {path: names - kept for path, names in removed.items()}
    dependencies: dict[str, set[str]] = {}
    for text in before.values():
        for name, words in projections[text][1].items():
            dependencies.setdefault(name, set()).update(words)
    sources = set(project.src.rglob("*.c")) | {p for p in outputs if p.suffix == ".c"}
    rows = []
    for source in sorted(sources):
        data = outputs.get(source, source)
        text = (data.read_bytes() if isinstance(data, Path) else data).decode()
        rows.append((source, source.read_text() if source.is_file() else "", text))
    shared = view(before), view(after), kept, lost, dependencies
    jobs_ = [rows[start : start + 32] for start in range(0, len(rows), 32)]
    with tui.task("Checking retained source dependencies", len(rows)):
        checked = (
            [_source_job(shared, job) for job in jobs_]
            if policy is None
            else pool.run(policy, _source_job, jobs_, shared)
        )
    refusals = [reason for batch in checked for reason in batch]
    if refusals:
        raise Held("headers", "headers.merge_only: " + "; ".join(refusals))
