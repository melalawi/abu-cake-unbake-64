"""Supply installed consumer prerequisites without importing obsolete umbrellas."""

from __future__ import annotations

import re
from pathlib import Path

from unbake.cdecl import declarations
from unbake.config import Held, Project
from unbake.decomp.draft_context import ordered_headers
from unbake.layout.header_context import Headers
from unbake.layout.split import Edit
from unbake.project.headers import Graph, Include, ProviderSet, scan
from unbake.typemap.split import required_providers

_MACRO = re.compile(r"^[ \t]*#[ \t]*define[ \t]+([A-Za-z_]\w*)(?:\([^\n]*?\))?[ \t]*(.*)", re.M)


def _without_comments(text: str) -> str:
    return re.sub(
        r'"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'|/\*.*?\*/|//[^\n]*',
        lambda m: re.sub(r"[^\n]", " ", m[0]) if m[0].startswith(("/*", "//")) else m[0],
        text,
        flags=re.S,
    )


def resolve(project: Project, headers: Headers, text: str, function: str = "", *, edits: tuple[Edit, ...] = ()) -> str:
    """Replace missing shared imports using live homes; preserve all non-include bytes."""
    contents = {**headers.texts, **{edit.path: edit.after for edit in edits}}
    graph = Graph.contents(contents, project.include, cache_root=project.cache)
    index = ProviderSet(graph)

    def find(name: str) -> Path | None:
        return graph.resolve(project.src / "_imports.c", Include(name, True, 0, 0)).target

    def obsolete(name: str) -> bool:
        from unbake.layout import index

        return name in index.load(project)["headers"] and find(name) is None

    # Mask only include directives. Comments, local macros and every function
    # byte remain intact, including conditionally compiled bodies.
    source = graph.rewrite_imports(_without_comments(text), lambda include, original: " " * len(original))
    try:
        local = declarations(source)
        blocked = local.typedefs | local.declared | {function}
        blocked_tags = local.tags
    except Held:
        # Layout folding owns diagnostics and recovery for richer imported C.
        # Never use a failed import parse to synthesize declarations.
        blocked = {function}
        blocked_tags = set()
    blocked_tags.update(blocked)
    blocked.update(m[1] for m in _MACRO.finditer(source))
    # Include conditions can rely on a macro formerly brought by the umbrella.
    conditions = "\n".join(re.findall(r"^[ \t]*#[ \t]*(?:if|elif|ifdef|ifndef)\b([^\n]*)", source, re.M))
    present = {
        path
        for directive in scan(text)
        if not obsolete(directive.name)
        and (path := graph.resolve(project.src / "_imports.c", directive).target) is not None
    }
    # Existing authored imports already supply their transitive providers.
    covered = set(graph.closure(present).paths)
    # Equivalent split components can already be supplied by an authored wrapper.
    provided = set()
    provided_tags = set()
    for path in covered:
        row = graph.projection(path).declarations
        provided.update(row.typedefs | row.declared)
        provided_tags.update(row.tags)
    required = required_providers(
        source + "\n" + conditions,
        index.names,
        index.tags,
        index.macros,
        blocked | provided,
        blocked_tags | provided_tags,
    )
    selected = required - covered

    def include(path: Path) -> str:
        return next(path.relative_to(root).as_posix() for root in project.include if path.is_relative_to(root))

    pending = list(selected | present)
    while pending:
        path = pending.pop()
        dependencies = {edge.target for edge in graph.edges(path) if edge.target is not None}
        row = graph.projection(path).declarations
        dependencies.update(
            required_providers(
                " ".join(row.uses | row.complete_uses),
                index.names,
                index.tags,
                index.macros,
                blocked | provided | row.typedefs | row.declared,
                blocked_tags | provided_tags | row.tags,
            )
        )
        for dep in dependencies - selected - covered:
            selected.add(dep)
            pending.append(dep)
    narrowed = text
    for directive in reversed(scan(text)):
        if obsolete(directive.name):
            narrowed = narrowed[: directive.start] + narrowed[directive.end :]
    # Source microcode/configuration defines must take effect before recovered
    # SDK imports. Stop before conditions: their tests may need an imported macro.
    prefix = re.match(
        r"(?:[ \t\r\n]|^[ \t]*#[ \t]*(?:define|undef|pragma)\b(?:\\\n|[^\n])*(?:\n|$))*",
        _without_comments(narrowed),
        re.M,
    )
    assert prefix is not None
    offset = prefix.end()
    # A provider already included later in the source is covered, but cannot
    # supply types to an earlier import. Order the unconditional include block
    # together with recovered imports. Configuration directives and conditions
    # delimit the block; every non-include byte retains its original position.
    clean = _without_comments(narrowed)
    block = re.match(r"(?:[ \t\r\n]|^[ \t]*#[ \t]*include\b[^\n]*(?:\n|$))*", clean[offset:], re.M)
    assert block is not None
    matches = [
        (match, path)
        for match in scan(clean)
        if offset <= match.start
        and match.end <= offset + block.end()
        and (path := graph.resolve(project.src / "_imports.c", match).target) is not None
    ]
    paths = dict.fromkeys([*sorted(selected), *(path for _, path in matches)])
    ordinary = [path for path in paths if path.name != "gbi.h"]
    ordered = [
        *ordered_headers(contents, aliases=index.macros, roots=ordinary),
        *(path for path in paths if path.name == "gbi.h"),
    ]
    directives: dict[Path, list[str]] = {path: [] for path in ordered}
    for path in sorted(selected):
        directives[path].append(f'#include "{include(path)}"')
    for match, path in matches:
        directives[path].append(narrowed[match.start : match.end])
    lines = [line for path in ordered for line in directives[path]]
    added = len(lines) - len(matches)
    for (match, _), line in reversed(list(zip(matches, lines[added:], strict=True))):
        narrowed = narrowed[: match.start] + line + narrowed[match.end :]
    return narrowed[:offset] + "".join(line + "\n" for line in lines[:added]) + narrowed[offset:]
