"""Order project headers by their shared typedef dependencies."""

from __future__ import annotations

import os
import re
from pathlib import Path

from unbake.cdecl import declarations
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


def ordered_headers(contents: dict[Path, str], *, aliases: dict[str, str] | None = None) -> list[Path]:
    """Put shared types before consumers even when headers omit includes.

    A typedef name a header uses needs one declaration before it: a provider the header includes is that
    declaration, else every provider precedes it. A complete use of a tag (directly or through an alias) also
    needs the tag's definition before it; a pointer to a tag needs nothing."""
    parsed = {}
    for path, text in contents.items():
        try:
            parsed[path] = declarations(text)
        except Held as error:
            raise Held("m2c", f"{path}: {error.reason}") from error
    providers: dict[str, set[Path]] = {}
    for path, header in parsed.items():
        for name in header.typedefs:
            providers.setdefault(name, set()).add(path)
    included = _includes(contents)
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
            target = (aliases or {}).get(alias, "")
            if re.fullmatch(r"(?:struct|union) \w+", target):
                complete.add(target.split()[1])
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

    for path in contents:
        visit(path)
    return ordered


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
    expanded = run_tool(command, project.root, "m2c")
    return re.sub(r"^\s*#\s*(?:line\s+)?\d+[^\n]*", "", expanded, flags=re.M)
