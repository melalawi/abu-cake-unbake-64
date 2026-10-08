"""Reuse an equal installed declaration instead of publishing a second provider.

A stable installed header owns each reused layout. Equality requires complete
declarators and each provider's transitive alias/tag identity, including member
names, order, widths, extents and qualifiers; size alone never authorizes reuse.
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
from unbake.process import named as cause_named
from unbake.project.headers import Graph


@dataclass(frozen=True)
class Catalog:
    tags: dict[str, tuple[str, int, int, tuple[str, ...]]]
    typedefs: dict[str, tuple[int, int, tuple[str, ...]]]
    guard: str | None
    unproved: frozenset[tuple[str, str]]

    def proved(self, namespace: str, name: str) -> bool:
        return self.guard is not None and (namespace, name) not in self.unproved


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
    # Context belongs to each declaration. An unrelated conditional macro
    # elsewhere in a guarded header does not change an unconditional layout.
    uncommented = re.sub(r"/\*.*?\*/|//[^\n]*", lambda m: re.sub(r"[^\n]", " ", m[0]), text, flags=re.S)
    directives = list(re.finditer(r"^[ \t]*#[ \t]*(\w+)([^\n]*)", uncommented, re.M))
    significant = [m for m in directives if m[1] != "include"]
    guard = (
        significant[0][2].strip()
        if len(significant) >= 3
        and significant[0][1] == "ifndef"
        and significant[1][1] == "define"
        and significant[1][2].strip() == significant[0][2].strip()
        and significant[-1][1] == "endif"
        and re.fullmatch(r"[A-Za-z_]\w*", significant[0][2].strip())
        else None
    )
    stack: list[int] = []
    conditional: list[tuple[int, int]] = []
    macros: set[str] = set()
    unknown = False
    for directive in significant:
        directive_kind, argument = directive[1], directive[2].strip()
        if directive_kind in {"if", "ifdef", "ifndef"}:
            if not stack and directive is not significant[0]:
                guard = None
            stack.append(directive.start())
        elif directive_kind == "endif":
            if not stack:
                guard = None
            else:
                start = stack.pop()
                if stack or guard is None:
                    conditional.append((start, directive.end()))
                elif directive is not significant[-1]:
                    guard = None
        elif directive_kind in {"define", "undef"}:
            if match := re.match(r"\w+", argument):
                macros.add(match[0])
        elif directive_kind in {"else", "elif"}:
            if len(stack) <= 1:
                guard = None
        else:
            unknown = True
    if stack:
        guard = None
    macros.discard(guard or "")
    unproved = set()
    for namespace, records in (("tag", tags), ("alias", typedefs)):
        for name, record in records.items():
            start, end, tokens = record[-3:]
            if unknown or set(tokens) & macros or any(start < last and end > first for first, last in conditional):
                unproved.add((namespace, name))
    return Catalog(tags, typedefs, guard, frozenset(unproved))


def _catalog_job(shared: Path | None, texts: list[str]) -> list[Catalog]:
    """Catalogs of TEXTS, each kept in the project cache by its text so a later landing reads it."""
    from unbake import cache

    if shared is None:
        return [_catalog(text) for text in texts]
    store = cache.Cache(shared)

    def one(text: str) -> Catalog:
        catalog: Catalog = store.value("provider-catalog", cache.key(text), cache.PICKLE, lambda: _catalog(text))
        return catalog

    return [one(text) for text in texts]


def catalogs_of(texts: list[str], host: Host | None, cache_root: Path | None, memo: dict[str, Catalog]) -> None:
    """Fill MEMO with the catalog of every text in TEXTS: one parse per distinct text, pooled over the workers."""
    from unbake import pool, tui

    todo = [text for text in dict.fromkeys(texts) if text not in memo]
    if not todo:
        return
    if host is None:
        memo.update((text, _catalog(text)) for text in todo)
        return
    batches = [todo[start : start + 64] for start in range(0, len(todo), 64)]
    with tui.task("Cataloguing header declarations", len(todo)):
        done = pool.run(host, _catalog_job, batches, cache_root)
    memo.update(zip(todo, (row for batch in done for row in batch), strict=True))


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
        cause_named(
            "headers.declaration.duplicate-shared-provider",
            f"headers.declaration.duplicate-shared-provider: {name}: {detail}; providers: "
            + ", ".join(map(str, labels)),
            owner="fold.provider_reuse",
            stage="structs",
        )
    )


def plan(
    project: Project,
    contents: dict[Path, str],
    versions: tuple[str, ...],
    *,
    changed: frozenset[Path] = frozenset(),
    cache: dict[str, Catalog] | None = None,
    host: Host | None = None,
) -> list[Edit]:
    """Plan one canonical guarded provider using already read effective bytes."""
    private = {path for path in contents if any(path.is_relative_to(root) for root in project.work_include)}
    cache = {} if cache is None else cache
    catalogs_of(list(contents.values()), host, project.cache, cache)
    catalogs = {path: cache[text] for path, text in contents.items()}
    providers: dict[str, list[Path]] = {}
    typedefs: dict[str, list[tuple[Path, tuple[str, ...]]]] = {}
    for path, catalog in catalogs.items():
        for name in catalog.tags:
            providers.setdefault(name, []).append(path)
        for name, (_, _, tokens) in catalog.typedefs.items():
            typedefs.setdefault(name, []).append((path, tokens))

    # Prefer the tagged declaration owner over an anonymous definition when
    # both providers are private overlays. A copied owner header can be
    # shadowing its installed home; choosing the anonymous consumer instead
    # creates reciprocal imports and removes prerequisites from both views.
    def alias_rank(home: Path, name: str) -> tuple[bool, bool, bool, str]:
        tokens = catalogs[home].typedefs[name][2]
        forward = len(tokens) == 5 and tokens[:2] in (("typedef", "struct"), ("typedef", "union"))
        return home in private, home in changed, not forward, home.as_posix()

    alias_homes = {
        name: min((home for home, _ in rows), key=lambda home: alias_rank(home, name))
        for name, rows in typedefs.items()
    }
    from unbake.fold.provider_identity import Identity

    graph = Graph.contents(contents, project.include)
    identity = Identity(contents, catalogs, graph)
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
            if (
                catalog.proved("tag", name)
                and catalogs[home].proved("tag", name)
                and catalog.guard == catalogs[home].guard
            ):
                continue  # Already one provider under native include-guard semantics.
            if (kind, tokens) != (other_kind, other_tokens) and not identity.equal("tag", name, path, home):
                _refuse(project, contents, name, [path, home], "conflicting complete declaration")
            if not catalog.proved("tag", name) or not catalogs[home].proved("tag", name):
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
            home = alias_homes[name]
            if (
                len(typedefs[name]) < 2
                or home == path
                or (
                    catalog.proved("alias", name)
                    and catalogs[home].proved("alias", name)
                    and catalog.guard == catalogs[home].guard
                )
            ):
                continue
            if tokens != catalogs[home].typedefs[name][2] and not identity.equal("alias", name, path, home):
                _refuse(project, contents, name, [path, home], "conflicting typedef dependency")
            if not catalog.proved("alias", name) or not catalogs[home].proved("alias", name):
                _refuse(project, contents, name, [path, home], "conditional or macro context is not proved")
            homes.add(home)
            required.update(tokens)
        if not homes:
            continue
        imported = set(graph.closure(homes).paths)
        if path in imported:
            _refuse(project, contents, path.name, [path, *sorted(homes)], "canonical import would form a cycle")
        # Follow typedef dependencies to prove that identical field spellings
        # do not hide different callback, scalar or aggregate interpretations.
        checked: set[str] = set()
        while pending := required & typedefs.keys() - checked:
            for name in sorted(pending):
                rows = typedefs[name]
                checked.add(name)
                if any(("alias", name) in catalogs[home].unproved for home, _ in rows):
                    _refuse(
                        project,
                        contents,
                        name,
                        [home for home, _ in rows],
                        "conditional or macro context is not proved",
                    )
                if len({tokens for _, tokens in rows}) != 1 and not all(
                    tokens == rows[0][1] or identity.equal("alias", name, rows[0][0], home) for home, tokens in rows[1:]
                ):
                    _refuse(project, contents, name, [home for home, _ in rows], "conflicting typedef dependency")
                required.update(rows[0][1])
        for name, (start, end, tokens) in catalog.typedefs.items():
            # Only the chosen owner can replace this spelling. A transitive
            # duplicate may itself be removed later in this plan; it is not a
            # certificate that the final include context still provides it.
            home = alias_homes[name]
            if home == path or home not in imported:
                continue
            shared = [home]
            if any(
                tokens != catalogs[home].typedefs[name][2] and not identity.equal("alias", name, path, home)
                for home in shared
            ):
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


def regenerated(
    project: Project,
    outputs: dict[Path, bytes | Path],
    contents: dict[Path, str],
    installed: dict[Path, str],
    *,
    cache: dict[str, Catalog] | None = None,
) -> dict[Path, bytes | Path]:
    """Use fold's owner plan on the complete retained and proposed header view.

    A regenerated wrapper can still be reached alongside an older header by a
    published source. Keep its canonical imports in this installation and in
    the manifest, so the following headers step cannot delete their owners.
    All native validation still consumes the normalized proposed bytes.
    """
    from unbake.layout import index

    proposed = {
        path: (data.read_bytes() if isinstance(data, Path) else data).decode()
        for path, data in outputs.items()
        if path.suffix == ".h"
    }
    effective = {**contents, **proposed}
    edits = plan(project, effective, tuple(project.versions), changed=frozenset(proposed), cache=cache)
    normalized = dict(outputs)
    for edit in edits:
        effective[edit.path] = edit.after
        proposed[edit.path] = edit.after
        normalized[edit.path] = edit.after.encode()
    needed = set(Graph.contents(effective, project.include).closure(proposed).paths)
    retained = needed & installed.keys() - proposed.keys()
    for path in retained:
        normalized[path] = effective[path].encode()
    if edits or retained:
        listing = normalized.get(index.path(project))
        if listing is not None:
            import json

            lookup = json.loads(listing.read_bytes() if isinstance(listing, Path) else listing)
            changes = {
                path.relative_to(project.include[0]).as_posix(): effective[path]
                for path in {*retained, *(edit.path for edit in edits)}
                if path.is_relative_to(project.include[0])
            }
            normalized[index.path(project)] = index.encoded(index.overlay(lookup, changes))
    return normalized
