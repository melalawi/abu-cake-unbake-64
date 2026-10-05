"""Render declaration views, rewrite imports and remove only index-listed outputs."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from unbake import pool
from unbake.config import Held, Host, Project
from unbake.layout import index, map, redeclarations
from unbake.typemap import storage

_INCLUDE = re.compile(r'^[ \t]*#[ \t]*include[ \t]*[<"]([^>"\n]+)[>"][^\n]*(?:\n|$)', re.M)


def spelled(text: str) -> set[str]:
    """Every name the source spells, including macro bodies; include lines and comments are not uses."""
    code = re.sub(r"/\*.*?\*/|//[^\n]*", " ", text, flags=re.S)
    code = re.sub(r"^[ \t]*#[ \t]*include[^\n]*", " ", code, flags=re.M)
    return set(re.findall(r"\b[A-Za-z_]\w*\b", code))


def _local_names(source: Path, text: str) -> set[str]:
    """Names the source declares itself before its first use of them; a later declaration needs the header."""
    first = redeclarations.declared(source, text)
    code = re.sub(r"/\*.*?\*/|//[^\n]*", lambda m: " " * len(m[0]), text, flags=re.S)
    code = re.sub(r"^[ \t]*#[ \t]*include[^\n]*", lambda m: " " * len(m[0]), code, flags=re.M)
    used: dict[str, int] = {}
    for match in re.finditer(r"\b[A-Za-z_]\w*\b", code):
        used.setdefault(match[0], match.start())
    return {name for name, start in first.items() if used.get(name, start) >= start}


def rewrite(
    source: Path,
    text: str,
    member: str,
    ownership: map.Map,
    lookup: dict[str, Any],
    *,
    previous: set[str],
    wanted: frozenset[str] = frozenset(),
) -> str:
    owner = ownership.owners.get(member)
    if owner is None:
        raise Held("layout", f"layout.member.{member}: source has no group")
    # A header is imported only for names the source does not already declare itself, or WANTED ones.
    tokens = spelled(text) - (_local_names(source, text) - wanted)
    homes = {owner.header} | {lookup["symbols"][name] for name in tokens if name in lookup["symbols"]}
    homes.update(home for name in tokens for home in lookup.get("type_headers", {}).get(name, ()))
    includes = "".join(f'#include "{home}"\n' for home in sorted(homes))
    first = True

    def replace(match: re.Match[str]) -> str:
        nonlocal first
        if match[1] not in previous and match[1] not in lookup["headers"]:
            return match[0]
        if first:
            first = False
            return includes
        return ""

    result = _INCLUDE.sub(replace, text)
    if first:
        result = includes + result
    return result


def imported(text: str, root: Path, outputs: Mapping[Path, bytes | Path]) -> list[str]:
    """Read the source's actual transitive includes, using staged header bytes."""
    result = []
    pending = [root / m[1] for m in _INCLUDE.finditer(text)]
    seen = set()
    while pending:
        path = pending.pop().resolve()
        if path in seen:
            continue
        seen.add(path)
        if not path.resolve().is_relative_to(root.resolve()):
            continue
        data = outputs.get(path)
        if isinstance(data, Path):
            body = data.read_text()
        elif isinstance(data, bytes):
            body = data.decode()
        elif path.is_file():
            body = path.read_text()
        else:
            continue
        result.append(body)
        for match in _INCLUDE.finditer(body):
            relative = (path.parent / match[1]).resolve()
            pending.append(relative if relative in outputs or relative.is_file() else root / match[1])
    return result


def source(
    project: Project,
    text: str,
    member: str,
    outputs: Mapping[Path, bytes | Path],
    *,
    ownership: map.Map | None = None,
    lookup: dict[str, Any] | None = None,
    previous: set[str] | None = None,
    disagreements: dict[str, tuple[str, str]] | None = None,
) -> str:
    if lookup is None:
        import json

        pending = outputs.get(index.path(project))
        lookup = json.loads(pending) if isinstance(pending, bytes) else index.load(project)
        if not isinstance(pending, bytes):
            from unbake.cdecl import declarations

            for path, data in outputs.items():
                if path.suffix != ".h" or not isinstance(data, bytes):
                    continue
                name = path.relative_to(project.include[0]).as_posix()
                lookup["headers"][name] = storage.digest(data)
                row = declarations(data.decode())
                for symbol in row.typedefs | row.declared | row.exports:
                    lookup["symbols"][symbol] = name
    ownership = ownership or map.load(project)
    previous = set(index.load(project)["headers"]) if previous is None else previous
    path = project.src / f"{member}.c"
    rewritten = rewrite(path, text, member, ownership, lookup, previous=previous)
    bodies = imported(rewritten, project.include[0], outputs)
    # A local declaration the imports cover only in part (a per-version conditional) also imports the homes of
    # its other names, so it is removed whole rather than refused.
    wanted = frozenset(redeclarations.uncovered(rewritten, bodies) & lookup["symbols"].keys())
    if wanted:
        rewritten = rewrite(path, text, member, ownership, lookup, previous=previous, wanted=wanted)
        bodies = imported(rewritten, project.include[0], outputs)
    text = rewritten
    text, _ = redeclarations.privatize_tags(text, bodies, member)
    return redeclarations.strip(text, bodies, disagreements)


# At least this many source chunks per worker, so uneven sources still spread over every worker.
@dataclass(frozen=True)
class _Rewrite:
    """What every source is rewritten against: the rendered headers (shared by the pool's workers)."""

    project: Project
    headers: dict[Path, bytes]
    ownership: map.Map
    lookup: dict[str, Any]
    previous: set[str]
    collect: bool


def _rewrite(job: _Rewrite, item: tuple[Path, str]) -> tuple[bytes, dict[str, tuple[str, str]] | None]:
    """Worker body: one source's rewritten bytes and, when collected, its removed differing declarations."""
    path, text = item
    found: dict[str, tuple[str, str]] | None = {} if job.collect else None
    data = source(
        job.project,
        text,
        path.stem,
        job.headers,
        ownership=job.ownership,
        lookup=job.lookup,
        previous=job.previous,
        disagreements=found,
    ).encode()
    return data, found


def render(
    project: Project, policy: Host, disagreements: dict[Path, dict[str, tuple[str, str]]] | None = None
) -> dict[Path, bytes]:
    """Every generated header, the header index and each source's rewritten include lines, by path.

    With DISAGREEMENTS, a source's local declaration that differs from its header's is removed and recorded
    per source, for the caller to prove; without it, such a source is refused."""
    import json

    from unbake.typemap import database, regeneration

    map.load(project)
    loaded = database.load(project, allow_stale=True)
    if loaded is None:
        raise Held("headers", "headers.types: no type solution; the types step must run first")
    # The loaded solution is memoised: rendering adds declaration evidence and alias fields, so it gets its own
    # top level and evidence map; every other record is only read.
    value = {**loaded, "declaration_evidence": dict(loaded.get("declaration_evidence", {}))}
    session = regeneration.Session(project, policy)
    # The types step rendered and installed this solution's headers; with the same inputs the render is reused,
    # so the headers it installs are byte for byte the ones the types step validated (and keyed its facts on).
    outputs = {
        path: data.read_bytes() if isinstance(data, Path) else data
        for path, data in session.render(value, lambda: database._render(project, value, policy, session)).items()
    }
    lookup = json.loads(outputs[index.path(project)])
    previous = set(index.load(project)["headers"])
    # Each source is rewritten on its own: its imports read only include/ (generated outputs or files), never
    # another source. The sources go to the worker pool; the shared views are loaded once per worker.
    headers = {path: data for path, data in outputs.items() if path.suffix != ".c"}
    items = list(session.sources.items())
    shared = _Rewrite(project, headers, session.ownership, lookup, previous, disagreements is not None)
    for (path, _), (data, found) in zip(items, pool.run(policy, _rewrite, items, shared), strict=True):
        outputs[path] = data
        if found and disagreements is not None:
            disagreements[path] = found
    return outputs


def units(project: Project, *, dry_run: bool = False) -> int:
    """Rewrite every version's split rows so private pools form one row per run."""
    from unbake.layout import split
    from unbake.layout import units as view

    changed = 0
    for version in project.versions:
        path = project.version(version).split
        text = split.read(path)
        merged = view.merge_pools(text)
        if merged != text:
            changed += 1
            if not dry_run:
                storage.write(path, merged.encode())
    return changed


def install(project: Project, outputs: dict[Path, bytes | Path], *, dry_run: bool = False) -> int:
    """Validate all content before the first write; stale paths come from the old index."""
    obsolete = index.headers(project) - outputs.keys()
    changed = {
        p: data
        for p, data in outputs.items()
        if not p.is_file() or p.read_bytes() != (data.read_bytes() if isinstance(data, Path) else data)
    }
    if not dry_run and (changed or obsolete):
        # The index is installed last, so it always describes complete views.
        for path in sorted(changed, key=lambda p: (p == index.path(project), str(p))):
            data = changed[path]
            if isinstance(data, Path):
                storage.install(path, data)
            else:
                storage.write(path, data)
        for path in obsolete:
            path.unlink(missing_ok=True)
    return len(changed) + len(obsolete)
