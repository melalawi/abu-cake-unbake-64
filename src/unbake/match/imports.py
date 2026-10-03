"""Supply installed consumer prerequisites without importing obsolete umbrellas."""

from __future__ import annotations

import re
from pathlib import Path

from unbake.decomp.draft_context import ordered_headers
from unbake.decomp.header_declarations import declaration_source, declarations
from unbake.layout.header_context import Headers
from unbake.layout.split import Edit
from unbake.project.cache import remembered
from unbake.project.config import Held, Project
from unbake.typemap.header_names import alias_types
from unbake.typemap.split import required_providers

_INCLUDE = re.compile(r'^[ \t]*#[ \t]*include[ \t]*[<"]([^>"\n]+)[>"][^\n]*', re.M)
_MACRO = re.compile(r"^[ \t]*#[ \t]*define[ \t]+([A-Za-z_]\w*)(?:\([^\n]*?\))?[ \t]*(.*)", re.M)
_UMBRELLAS = {"typemap.h", "shared/typemap.h", "prototypes.h", "shared/prototypes.h"}


def _without_comments(text: str) -> str:
    return re.sub(
        r'"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'|/\*.*?\*/|//[^\n]*',
        lambda m: re.sub(r"[^\n]", " ", m[0]) if m[0].startswith(("/*", "//")) else m[0],
        text,
        flags=re.S,
    )


class Providers:
    """Installed homes for the same name closure used by the split generator."""

    def __init__(self, contents: dict[Path, str]):
        self.names: dict[str, set[Path]] = {}
        self.tags: dict[str, set[Path]] = {}
        self.macros: dict[str, str] = {}
        for path, text in contents.items():
            text = _without_comments(text)
            if path.name in ("typemap.h", "prototypes.h"):
                continue
            row = declarations(text)
            macros = {m[1]: m[2] for m in _MACRO.finditer(text) if not m[1].startswith("UNBAKE_")}
            for name in row.typedefs | row.declared | macros.keys():
                self.names.setdefault(name, set()).add(path)
            for name in row.tags:
                self.tags.setdefault(name, set()).add(path)
            # Enum constants are exports too; the declaration reader skips their values.
            for enum in re.findall(r"\benum\b[^{};]*\{([^{}]*)\}", declaration_source(text)):
                for member in enum.split(","):
                    name = re.match(r"\s*([A-Za-z_]\w*)", member)
                    if name:
                        self.names.setdefault(name[1], set()).add(path)
            self.macros.update(alias_types(text))
            self.macros.update(macros)
        for catalogue in (self.names, self.tags):
            for name, paths in catalogue.items():
                # Authored wrappers and their split components share guards. Use
                # the narrow component rather than bringing the wrapper's other types.
                split = {p for p in paths if p.parent.name in ("types", "decls")}
                catalogue[name] = split or paths


def resolve(project: Project, headers: Headers, text: str, function: str = "", *, edits: tuple[Edit, ...] = ()) -> str:
    """Replace missing shared imports using live homes; preserve all non-include bytes."""
    contents = {**headers.texts, **{edit.path: edit.after for edit in edits}}
    index = remembered("imports.providers", tuple(sorted(contents.items())), lambda: Providers(contents), keep=2)

    def find(name: str) -> Path | None:
        return next((root / name for root in project.include if root / name in contents), None)

    def obsolete(name: str) -> bool:
        return name in _UMBRELLAS or (name.startswith("shared/") and find(name) is None)

    # Mask only include directives. Comments, local macros and every function
    # byte remain intact, including conditionally compiled bodies.
    source = _INCLUDE.sub(lambda m: " " * len(m[0]), _without_comments(text))
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
        for name in _INCLUDE.findall(_without_comments(text))
        if not obsolete(name) and (path := find(name)) is not None
    }
    # Existing authored imports already supply their transitive providers.
    covered = set(present)
    pending = list(present)
    while pending:
        path = pending.pop()
        for name in _INCLUDE.findall(contents.get(path, "")):
            dep = find(name)
            if dep is not None and dep not in covered:
                covered.add(dep)
                pending.append(dep)
    # Equivalent split components can already be supplied by an authored wrapper.
    provided = set()
    provided_tags = set()
    for path in covered:
        row = declarations(contents.get(path, ""))
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
        dependencies = {dep for name in _INCLUDE.findall(contents[path]) if (dep := find(name)) is not None}
        row = declarations(contents[path])
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
    ordinary = {path: contents[path] for path in sorted(selected) if path.name != "gbi.h"}
    ordered = [
        *ordered_headers(ordinary, aliases=index.macros),
        *(path for path in sorted(selected) if path.name == "gbi.h"),
    ]
    directives = "".join(f'#include "{include(path)}"\n' for path in ordered)
    narrowed = text
    for match in reversed(list(_INCLUDE.finditer(_without_comments(text)))):
        if obsolete(match[1]):
            narrowed = narrowed[: match.start()] + narrowed[match.end() :]
    # Source microcode/configuration defines must take effect before recovered
    # SDK imports. Stop before conditions: their tests may need an imported macro.
    prefix = re.match(
        r"(?:[ \t\r\n]|^[ \t]*#[ \t]*(?:define|undef|pragma)\b(?:\\\n|[^\n])*(?:\n|$))*",
        _without_comments(narrowed),
        re.M,
    )
    assert prefix is not None
    offset = prefix.end()
    return narrowed[:offset] + directives + narrowed[offset:]
