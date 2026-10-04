"""Order project headers by their shared typedef dependencies."""

from __future__ import annotations

import re
from pathlib import Path

from unbake.decomp.header_declarations import declarations
from unbake.config import Held, Host, Project


def _clean(text: str) -> str:
    return re.sub(r'/\*.*?\*/|//[^\n]*|"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'', " ", text, flags=re.S)


def _typedefs(text: str) -> set[str]:
    return declarations(text).typedefs


def ordered_headers(contents: dict[Path, str], *, aliases: dict[str, str] | None = None) -> list[Path]:
    """Put shared types before consumers even when headers omit includes."""
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
    dependencies = {
        path: {
            provider
            for name in header.uses - header.typedefs
            for provider in providers.get(name, set())
            if provider != path
        }
        for path, header in parsed.items()
    }
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
            dependencies[path].update(provider for provider in tag_providers.get(name, set()) if provider != path)
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
    """Use the selected source unit's make recipe for context preprocessing."""
    from unbake.decomp.explain import _absolute_includes
    from unbake.process import run_tool
    from unbake.project import makefile
    from unbake.project_tools.sn64_cc import partition_flags

    unit = project.src / (function + ".c")
    recipe = makefile.recipe(project)
    flags = _absolute_includes(project, makefile.flags(project, version, unit))
    if project.compiler_for(unit).kind == "sn64":
        try:
            options, _ = partition_flags(flags)
        except ValueError as error:
            raise Held("m2c", f"compiler.cflags: {error}") from error
    else:
        # Native IDO code-generation options are not SN64 driver options.
        options = []
        pending = iter(flags)
        for flag in pending:
            if flag in ("-I", "-D", "-U", "-include", "-isystem"):
                value = next(pending, None)
                if value is None:
                    raise Held("m2c", f"compiler.cflags.{flag}: missing preprocessing argument")
                options.extend((flag, value))
            elif flag.startswith(("-I", "-D", "-U")):
                options.append(flag)
    cpp = makefile.host_executable(policy, recipe.cpp or "policy:cpp", "cpp")
    expanded = run_tool([cpp, *recipe.cppflags, *options, "-DNON_MATCHING=1", str(source)], project.root, "m2c")
    return re.sub(r"^\s*#\s*(?:line\s+)?\d+[^\n]*", "", expanded, flags=re.M)
