"""Render declaration views, rewrite imports and remove only index-listed outputs."""

from __future__ import annotations

import copy
import re
from pathlib import Path
from typing import Any

from unbake.layout import index, map, redeclarations
from unbake.project.config import Held, Policy, Project
from unbake.typemap import storage

_INCLUDE = re.compile(r'^[ \t]*#[ \t]*include[ \t]*[<"]([^>"\n]+)[>"][^\n]*(?:\n|$)', re.M)


def _spelled(text: str) -> set[str]:
    """Every name the source spells, including macro bodies; include lines and comments are not uses."""
    code = re.sub(r"/\*.*?\*/|//[^\n]*", " ", text, flags=re.S)
    code = re.sub(r"^[ \t]*#[ \t]*include[^\n]*", " ", code, flags=re.M)
    return set(re.findall(r"\b[A-Za-z_]\w*\b", code))


def _local_names(text: str) -> set[str]:
    from unbake.decomp.header_declarations import declarations

    return {
        name
        for start, end in redeclarations.spans(text)
        for variant in redeclarations.variants(text[start:end])
        for name in declarations(variant).declared
    }


def rewrite(text: str, member: str, ownership: map.Map, lookup: dict[str, Any], *, previous: set[str]) -> str:
    owner = ownership.owners.get(member)
    if owner is None:
        raise Held("layout", f"layout.member.{member}: source has no group")
    # A header is imported only for names the source does not already declare itself.
    tokens = _spelled(text) - _local_names(text)
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


def imported(text: str, root: Path, outputs: dict[Path, bytes | Path]) -> list[str]:
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
    outputs: dict[Path, bytes | Path],
    *,
    ownership: map.Map | None = None,
    lookup: dict[str, Any] | None = None,
    previous: set[str] | None = None,
) -> str:
    if lookup is None:
        import json

        pending = outputs.get(index.path(project))
        lookup = json.loads(pending) if isinstance(pending, bytes) else index.load(project)
        if not isinstance(pending, bytes):
            from unbake.decomp.header_declarations import declarations

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
    text = rewrite(text, member, ownership, lookup, previous=previous)
    bodies = imported(text, project.include[0], outputs)
    text, _ = redeclarations.privatize_tags(text, bodies, member)
    return redeclarations.strip(text, bodies)


def run(project: Project, policy: Policy | None = None, *, dry_run: bool = False) -> int:
    from unbake.typemap import database, regeneration

    map.load(project)
    value = copy.deepcopy(database.load(project, allow_stale=True))
    assert value is not None
    session = regeneration.Session(project, policy)
    outputs = database._render(project, value, policy, session)
    import json

    lookup = json.loads(outputs[index.path(project)])  # type: ignore[arg-type]
    previous = set(index.load(project)["headers"])
    for path, text in session.sources.items():
        outputs[path] = source(
            project, text, path.stem, outputs, ownership=session.ownership, lookup=lookup, previous=previous
        ).encode()
    count = install(project, outputs, dry_run=dry_run)
    if not dry_run and "inputs_sha256" in value:
        value["rendered_sha256"] = {
            storage.relative(project, p): storage.digest(data)
            for p, data in outputs.items()
            if isinstance(data, bytes) and p not in session.sources
        }
        for target, data in outputs.items():
            relative = storage.relative(project, target)
            if isinstance(data, bytes) and relative in value["inputs_sha256"]:
                value["inputs_sha256"][relative] = storage.digest(data)
        path = project.build / "types/database.json"
        data = storage.encoded(value)
        if path.read_bytes() != data:
            storage.write(path, data)
            count += 1
    return count


def install(project: Project, outputs: dict[Path, bytes | Path], *, dry_run: bool = False) -> int:
    """Validate all content before the first write; stale paths come from the old index."""
    obsolete = index.headers(project) - outputs.keys()
    changed = {
        p: data
        for p, data in outputs.items()
        if not p.is_file() or p.read_bytes() != (data.read_bytes() if isinstance(data, Path) else data)
    }
    if not dry_run:
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
