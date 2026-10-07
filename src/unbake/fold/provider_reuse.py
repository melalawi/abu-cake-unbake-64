"""Reuse an equal installed declaration instead of publishing a second provider.

A stable installed header owns each reused layout. Equality requires the complete
declaration tokens and their typedef dependencies, including field names and
qualifiers; matching size or a compatible prefix never authorizes reuse.
The caller parses the staged context and proves publication before any writes.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path

from unbake.cdecl import NAME_TOKEN, declaration_source, declarations
from unbake.config import Held, Host, Project
from unbake.layout import redeclarations
from unbake.layout.split import Edit
from unbake.project.headers import include_closure


@dataclass(frozen=True)
class Catalog:
    tags: dict[str, tuple[str, int, int, tuple[str, ...]]]
    typedefs: dict[str, tuple[int, int, tuple[str, ...]]]
    guard: str | None


def _catalog(text: str) -> Catalog:
    clean = declaration_source(text)
    tags = {}
    for name, bodies in redeclarations.tag_definitions(text).items():
        if len(bodies) != 1:
            continue
        start, end = bodies[0]
        kind = re.search(r"\b(struct|union)\s+" + re.escape(name) + r"\s*$", clean[:start])
        if kind is not None:
            tags[name] = (kind[1], start, end, tuple(NAME_TOKEN.findall(clean[start:end])))
    typedefs = {}
    for start, end in redeclarations.spans(text):
        row = clean[start:end]
        if re.match(r"typedef\b", row):
            tokens = tuple(NAME_TOKEN.findall(row))
            for name in declarations(row).typedefs:
                typedefs[name] = (start, end, tokens)
    # Other conditionals and local macro definitions require version-specific
    # proof. Keep that refusal instead of treating a raw catalogue as evidence.
    uncommented = re.sub(r"/\*.*?\*/|//[^\n]*", "", text, flags=re.S)
    directives = re.findall(r"^[ \t]*#[ \t]*(\w+)([^\n]*)", uncommented, re.M)
    significant = [(kind, arg.strip()) for kind, arg in directives if kind != "include"]
    guarded = (
        len(significant) == 3
        and significant[0][0] == "ifndef"
        and significant[1] == ("define", significant[0][1])
        and significant[2][0] == "endif"
        and re.fullmatch(r"[A-Za-z_]\w*", significant[0][1]) is not None
    )
    return Catalog(tags, typedefs, significant[0][1] if guarded else None)


def _refuse(project: Project, contents: dict[Path, str], name: str, paths: list[Path], detail: str) -> None:
    labels = []
    for path in paths:
        label = path.relative_to(project.root) if path.is_relative_to(project.root) else path
        text = contents[path]
        match = re.search(r"\b(?:struct|union)\s+" + re.escape(name) + r"\s*\{", declaration_source(text))
        offset = (
            match.start()
            if match
            else next(
                (start for start, end in redeclarations.spans(text) if name in declarations(text[start:end]).typedefs),
                0,
            )
        )
        line = text.count("\n", 0, offset) + 1
        labels.append(f"{label}:{line}")
    raise Held(
        "structs",
        f"headers.declaration.duplicate-shared-provider: {name}: {detail}; providers: " + ", ".join(map(str, labels)),
    )


def plan(
    project: Project,
    contents: dict[Path, str],
    versions: tuple[str, ...],
    *,
    changed: frozenset[Path] = frozenset(),
    cache: dict[str, Catalog] | None = None,
) -> list[Edit]:
    """Plan one canonical guarded provider using already read effective bytes."""
    private = {path for path in contents if any(path.is_relative_to(root) for root in project.work_include)}
    cache = {} if cache is None else cache
    catalogs = {}
    for path, text in contents.items():
        if text not in cache:
            cache[text] = _catalog(text)
        catalogs[path] = cache[text]
    providers: dict[str, list[Path]] = {}
    typedefs: dict[str, list[tuple[Path, tuple[str, ...]]]] = {}
    for path, catalog in catalogs.items():
        for name in catalog.tags:
            providers.setdefault(name, []).append(path)
        for name, (_, _, tokens) in catalog.typedefs.items():
            typedefs.setdefault(name, []).append((path, tokens))
    edits = []
    for path in sorted(contents):
        catalog = catalogs[path]
        removed = []
        homes: set[Path] = set()
        required: set[str] = set()
        for name, (kind, start, end, tokens) in catalog.tags.items():
            candidates = sorted(providers[name], key=lambda home: (home in private, home in changed, home.as_posix()))
            if len(candidates) < 2 or path == candidates[0]:
                continue
            home = candidates[0]
            other_kind, _, _, other_tokens = catalogs[home].tags[name]
            if catalog.guard is not None and catalog.guard == catalogs[home].guard:
                continue  # Already one provider under native include-guard semantics.
            if (kind, tokens) != (other_kind, other_tokens):
                _refuse(project, contents, name, [path, home], "conflicting complete declaration")
            if not catalog.guard or not catalogs[home].guard:
                _refuse(project, contents, name, [path, home], "conditional or macro context is not proved")
            homes.add(home)
            required.update(tokens)
            # Retain the tag/typedef declaration as a forward; only its complete
            # body moves to the prior provider. Other declarations stay owned.
            removed.append((start, end))
        # Callback/scalar/forward typedefs need an owner even when this header
        # contains no aggregate definition. Older native compilers reject an
        # identical repeated typedef as well.
        for name, (_start, _end, tokens) in catalog.typedefs.items():
            candidates = sorted(
                (home for home, _ in typedefs[name]),
                key=lambda home: (home in private, home in changed, home.as_posix()),
            )
            home = candidates[0]
            if len(candidates) < 2 or home == path or catalog.guard == catalogs[home].guard:
                continue
            if tokens != catalogs[home].typedefs[name][2]:
                _refuse(project, contents, name, [path, home], "conflicting typedef dependency")
            if not catalog.guard or not catalogs[home].guard:
                _refuse(project, contents, name, [path, home], "conditional or macro context is not proved")
            homes.add(home)
            required.update(tokens)
        if not homes:
            continue
        imported = include_closure(contents, tuple(project.include), homes)
        if path in imported:
            _refuse(project, contents, path.name, [path, *sorted(homes)], "canonical import would form a cycle")
        # Follow typedef dependencies to prove that identical field spellings
        # do not hide different callback, scalar or aggregate interpretations.
        checked: set[str] = set()
        while pending := required & typedefs.keys() - checked:
            for name in sorted(pending):
                rows = typedefs[name]
                checked.add(name)
                if len({tokens for _, tokens in rows}) != 1:
                    _refuse(project, contents, name, [home for home, _ in rows], "conflicting typedef dependency")
                required.update(rows[0][1])
        for name, (start, end, tokens) in catalog.typedefs.items():
            shared = [catalogs[home].typedefs[name][2] for home in imported if name in catalogs[home].typedefs]
            if not shared:
                continue
            if any(tokens != row for row in shared):
                _refuse(project, contents, name, [path, *sorted(homes)], "conflicting imported typedef")
            removed = [(left, right) for left, right in removed if not (start <= left and right <= end)]
            removed.append((start, end))
        after = contents[path]
        removed = [
            (start, end)
            for start, end in set(removed)
            if not any(left <= start and end <= right and (left, right) != (start, end) for left, right in removed)
        ]
        for start, end in sorted(removed, reverse=True):
            after = after[:start] + after[end:]
        imports = []
        for home in sorted(homes):
            relative = next(home.relative_to(root).as_posix() for root in project.include if home.is_relative_to(root))
            directive = f'#include "{relative}"\n'
            if directive.strip() not in after:
                imports.append(directive)
        # Import inside the private guard, before any remaining uses.
        define = re.search(r"^[ \t]*#[ \t]*define[^\n]*\n", after, re.M)
        assert define is not None
        after = after[: define.end()] + "".join(imports) + after[define.end() :]
        edits.append(Edit(path, contents[path], after, versions))
    return edits


@contextmanager
def view(project: Project, host: Host, versions: tuple[str, ...]) -> Iterator[Project]:
    """Compile comparisons against the same staged ownership as publication."""
    from unbake import atomic, scratch
    from unbake.layout.header_context import Headers
    from unbake.project.headers import include_headers

    contents = Headers.contents(project)
    edits = plan(project, contents, versions)
    if not edits:
        yield project
        return
    with scratch.temporary(host, project, "fold", prefix="shared-providers-") as temporary:
        root = Path(temporary) / "include"
        for edit in edits:
            relative = next(edit.path.relative_to(home) for home in project.include if edit.path.is_relative_to(home))
            atomic.text(root / relative, edit.after)
        for path, name in include_headers(project):
            mirror = root / name
            if not mirror.exists():
                mirror.parent.mkdir(parents=True, exist_ok=True)
                mirror.symlink_to(path)
        yield replace(project, work_include=(root, *project.work_include))
