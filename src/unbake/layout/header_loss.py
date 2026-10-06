"""Refuse lost published declaration dependencies before any header writer installs bytes."""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from pathlib import Path

from unbake.cdecl import declaration_source, declarations
from unbake.config import Held, Project

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


def _included(project: Project, source: Path, text: str, contents: Mapping[Path, str]) -> set[str]:
    """Resolve an include closure entirely within the old or proposed view."""
    pending = [(source, text)]
    seen: set[Path] = set()
    names: set[str] = set()
    while pending:
        parent, body = pending.pop()
        for name in _INCLUDE.findall(body):
            for root in (parent.parent, *project.include):
                path = (root / name).resolve()
                if path not in contents:
                    continue
                if path not in seen:
                    seen.add(path)
                    names.update(declared(contents[path]))
                    pending.append((path, contents[path]))
                break
    return names


def check(
    project: Project,
    outputs: Mapping[Path, bytes | Path],
    *,
    obsolete: Iterable[Path] = (),
) -> None:
    """Check the complete proposed tree, including unchanged headers and rewritten sources.

    Follow old declaration dependencies as well as body uses: losing a field's
    typedef breaks a published unit even when the body only spells its owner.
    Checking each source also supplies the concrete consumer in every refusal.
    """
    from unbake.layout import redeclarations
    from unbake.typemap.declaration_evidence import units

    removed_paths = set(obsolete)
    before = {path: path.read_text() for root in project.include for path in root.rglob("*.h")}
    after = {path: text for path, text in before.items() if path not in removed_paths}
    for path, data in outputs.items():
        if path.suffix == ".h":
            after[path] = (data.read_bytes() if isinstance(data, Path) else data).decode()
    changed = {path for path, text in before.items() if after.get(path) != text}
    if not changed:
        return
    kept = set().union(*(declared(text) for text in after.values()))
    removed = {path: declared(before[path]) - declared(after.get(path, "")) for path in sorted(changed)}
    if not any(removed.values()):
        return
    lost = {path: names - kept for path, names in removed.items()}
    dependencies: dict[str, set[str]] = {}
    for unit in units(before):
        for name in declared(unit.text):
            dependencies.setdefault(name, set()).update(_words(unit.text))
    refusals = []
    sources = set(project.src.rglob("*.c")) | {p for p in outputs if p.suffix == ".c"}
    for source in sorted(sources):
        data = outputs.get(source, source)
        text = (data.read_bytes() if isinstance(data, Path) else data).decode()
        # Source-owned declarations can satisfy a dependency in the final tree.
        # Keep function-declaration uses explicit, as in the headers step's
        # original merge-only contract; a sole definition is already excluded.
        provided = set().union(
            *(
                declarations(variant).typedefs
                for start, end in redeclarations.spans(text)
                for variant in redeclarations.variants(text[start:end])
            )
        )
        provided |= {f"{kind} {name}" for kind, name in _TAG.findall(declaration_source(text))}
        wanted = _words(text) - provided
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
        # A surviving declaration must still be reachable if it was reachable
        # before; a copy in an unrelated header cannot mask a destructive move.
        old_text = source.read_text() if source.is_file() else ""
        old_names = _included(project, source, old_text, before)
        new_names = _included(project, source, text, after)
        unreachable = (old_names - new_names) & wanted & kept
        if unreachable:
            refusals.append(f"{source}: would lose reachable declarations {', '.join(sorted(unreachable))}")
    if refusals:
        raise Held("headers", "headers.merge_only: " + "; ".join(refusals))
