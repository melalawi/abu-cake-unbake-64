"""Order project headers by their shared typedef dependencies."""

from __future__ import annotations

import re
from pathlib import Path

from unbake.project.config import Held


def _clean(text: str) -> str:
    return re.sub(r'/\*.*?\*/|//[^\n]*|"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'', " ", text, flags=re.S)


def _typedefs(text: str) -> set[str]:
    # Hide aggregate bodies so member semicolons cannot end a typedef.
    depth = 0
    outer = []
    for token in re.findall(r"[A-Za-z_]\w*|\S", text):
        if token == "{":
            depth += 1
        elif token == "}":
            depth -= 1
        elif depth == 0:
            outer.append(token)
    names = set()
    for declaration in re.findall(r"\btypedef\b([^;]+);", " ".join(outer)):
        declaration = re.sub(r"\[[^\]]*\]", "", declaration)
        names.update(re.findall(r"\(\s*\*\s*([A-Za-z_]\w*)\s*\)", declaration))
        names.update(re.findall(r"\b([A-Za-z_]\w*)\s*(?=,|$)", declaration))
    return names


def ordered_headers(contents: dict[Path, str]) -> list[Path]:
    """Put shared types before consumers even when headers omit includes."""
    clean = {path: _clean(text) for path, text in contents.items()}
    providers: dict[str, set[Path]] = {}
    declared = {path: _typedefs(text) for path, text in clean.items()}
    for path, names in declared.items():
        for name in names:
            providers.setdefault(name, set()).add(path)
    dependencies = {
        path: {
            provider
            for name in re.findall(r"\b[A-Za-z_]\w*\b", text)
            if name not in declared[path]
            for provider in providers.get(name, set())
            if provider != path
        }
        for path, text in clean.items()
    }
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
        names = _typedefs(text) | set(re.findall(r"\b(?:struct|union|enum)\s+(\w+)\s*\{", text))
        for declaration in re.findall(r"\bextern\b([^;]+);", text):
            declaration = re.sub(r"\[[^\]]*\]", "", declaration)
            function = re.search(r"\b([A-Za-z_]\w*)\s*\(", declaration)
            if function is not None:
                names.add(function[1])
            else:
                names.update(re.findall(r"\b([A-Za-z_]\w*)\s*(?=,|$)", declaration))
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
