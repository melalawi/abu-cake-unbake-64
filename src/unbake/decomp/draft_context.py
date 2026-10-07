"""Order project headers by their shared typedef dependencies."""

from __future__ import annotations

import os
import re
from collections.abc import Iterable
from pathlib import Path

from unbake.cdecl import Declarations, declarations
from unbake.config import Held, Host, Project


def _clean(text: str) -> str:
    return re.sub(r'/\*.*?\*/|//[^\n]*|"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'', " ", text, flags=re.S)


def _typedefs(text: str) -> set[str]:
    return declarations(text).typedefs


_INCLUDE = re.compile(r'^[ \t]*#[ \t]*include[ \t]*"([^"\n]+)"', re.M)


def _includes(contents: dict[Path, str]) -> dict[Path, set[Path]]:
    """Each header's include closure among CONTENTS: a quoted include names a header relative to the including
    one, or (without "..") by its trailing path."""
    paths = {os.path.normpath(path): path for path in contents}
    direct: dict[Path, set[Path]] = {}
    for path, text in contents.items():
        found = set()
        for name in _INCLUDE.findall(text):
            beside = paths.get(os.path.normpath(path.parent / name))
            if beside is not None:
                found.add(beside)
            elif ".." not in Path(name).parts:
                tail = Path(name).parts
                found.update(other for other in contents if other.parts[-len(tail) :] == tail)
        direct[path] = found - {path}
    closure: dict[Path, set[Path]] = {}
    for path in contents:
        seen: set[Path] = set()
        pending = [*direct[path]]
        while pending:
            other = pending.pop()
            if other not in seen:
                seen.add(other)
                pending.extend(direct[other])
        closure[path] = seen - {path}
    return closure


def ordered_headers(
    contents: dict[Path, str], *, aliases: dict[str, str] | None = None, roots: Iterable[Path] | None = None
) -> list[Path]:
    """Put shared types before consumers even when headers omit includes.

    A typedef name a header uses needs one declaration before it: a provider the header includes is that
    declaration, else every provider precedes it. A complete use of a tag (directly or through an alias) also
    needs the tag's definition before it; a pointer to a tag needs nothing.
    When ROOTS are supplied, order those includes by the declarations their
    include closures provide, parsing each selected header only once."""
    from unbake.typemap.header_names import alias_types

    parsed = {}
    mapping = {}
    included = _includes(contents)
    paths = list(dict.fromkeys(contents if roots is None else roots))
    selected = set(contents) if roots is None else {dep for path in paths for dep in {path} | included[path]}
    for path in contents:
        if path not in selected:
            continue
        text = contents[path]
        try:
            parsed[path] = declarations(text)
            mapping.update(alias_types(text))
        except Held as error:
            raise Held("m2c", f"{path}: {error.reason}") from error
    mapping.update(aliases or {})
    if roots is not None:
        effective = {}
        for path in paths:
            headers = [parsed[dep] for dep in {path} | included[path]]
            effective[path] = Declarations(
                typedefs=set().union(*(header.typedefs for header in headers)),
                uses=set().union(*(header.uses for header in headers)),
                tags=set().union(*(header.tags for header in headers)),
                complete_uses=set().union(*(header.complete_uses for header in headers)),
                complete_alias_uses=set().union(*(header.complete_alias_uses for header in headers)),
            )
        parsed = effective
    providers: dict[str, set[Path]] = {}
    for path, header in parsed.items():
        for name in header.typedefs:
            providers.setdefault(name, set()).add(path)
    dependencies: dict[Path, set[Path]] = {}
    for path, header in parsed.items():
        dependencies[path] = set()
        for name in header.uses - header.typedefs:
            offered = providers.get(name, set()) - {path}
            dependencies[path] |= (offered & included[path]) or offered
    tag_providers: dict[str, set[Path]] = {}
    for path, header in parsed.items():
        for name in header.tags:
            tag_providers.setdefault(name, set()).add(path)
    for path, header in parsed.items():
        complete = set(header.complete_uses)
        for alias in header.complete_alias_uses:
            seen = set()
            target = alias
            while target in mapping and target not in seen:
                seen.add(target)
                target = re.sub(r"\b(?:const|volatile|restrict|__restrict|__restrict__)\b", "", mapping[target]).strip()
            tag = re.fullmatch(r"(?:struct|union)\s+(\w+)(?:\s*\[[^]]*\])*", target)
            if tag:
                complete.add(tag[1])
        for name in complete - header.tags:
            offered = tag_providers.get(name, set()) - {path}
            dependencies[path] |= (offered & included[path]) or offered
    ordered: list[Path] = []
    active: list[Path] = []
    visited: set[Path] = set()

    def visit(path: Path) -> None:
        if path in active:
            cycle = [*active[active.index(path) :], path]
            raise Held("m2c", "cyclic shared type context: " + " -> ".join(map(str, cycle)))
        if path in visited:
            return
        active.append(path)
        for dependency in sorted(dependencies[path]):
            visit(dependency)
        active.pop()
        visited.add(path)
        ordered.append(path)

    for path in paths:
        visit(path)
    return ordered


def ordered_declarations(text: str, path: Path, *, aliases: dict[str, str] | None = None) -> str:
    """Order individual generated declarations, including providers in the same header."""
    from unbake.typemap.header_names import alias_types
    from unbake.typemap.split import statements

    try:
        parts = statements(text)
        contents = {}
        cursor = 0
        for index, part in enumerate(parts):
            start = text.index(part, cursor)
            # The splitter skips leading comments. Keep provenance attached
            # to the declaration it describes when moving that declaration.
            contents[Path(str(index))] = text[cursor:start] + part
            cursor = start + len(part)
        mapping = {**alias_types(text), **(aliases or {})}
        return "\n".join(contents[key] for key in ordered_headers(contents, aliases=mapping)) + text[cursor:]
    except Held as error:
        raise Held("m2c", f"{path}: {error.reason}") from error


def required_headers(contents: dict[Path, str], output: str) -> set[Path]:
    """Select declaration providers and their transitive type dependencies."""
    providers: dict[str, Path] = {}
    selected: set[Path] = set()
    for path in ordered_headers(contents):
        text = _clean(contents[path])
        header = declarations(contents[path])
        names = header.typedefs | header.exports
        # Keep one primitive type prelude for standalone scalar drafts.
        if names and "{" not in text and not selected:
            selected.add(path)
        for name in names:
            providers.setdefault(name, path)
    pending = [output, *(contents[path] for path in selected)]
    while pending:
        for name in re.findall(r"\b[A-Za-z_]\w*\b", _clean(pending.pop())):
            provider = providers.get(name)
            if provider is not None and provider not in selected:
                selected.add(provider)
                pending.append(contents[provider])
    return selected


def preprocess_context(source: Path, project: Project, policy: Host, version: str, function: str) -> str:
    """Preprocess draft context with the unit's own preprocessor options, without line markers."""
    from unbake.compilers import drivers
    from unbake.process import run_tool

    command = drivers.preprocess_command(project, str(policy.cpp), version, function, source, non_matching=True)
    expanded = run_tool(command, project.root, "m2c", temporary_root=project.build)
    return re.sub(r"^\s*#\s*(?:line\s+)?\d+[^\n]*", "", expanded, flags=re.M)
