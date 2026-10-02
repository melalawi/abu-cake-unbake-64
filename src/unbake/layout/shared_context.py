"""Order shared declaration homes and generated layouts by complete-type dependencies."""

from __future__ import annotations

import re
from pathlib import Path

from unbake.decomp.draft_context import _clean, _typedefs
from unbake.layout.structs import Field, held
from unbake.layout.structs_parser import Parser


def order(contents: dict[Path, str]) -> list[Path]:
    """A by-value shared consumer follows its provider; tag pointers need no definition."""
    aliases = {path: _typedefs(_clean(text)) for path, text in contents.items()}
    providers: dict[str, set[Path]] = {}
    for path, names in aliases.items():
        for name in names:
            providers.setdefault(name, set()).add(path)
    dependencies = {
        path: {
            home
            for name in re.findall(r"\b\w+\b", _clean(text))
            if name not in aliases[path]
            for home in providers.get(name, set())
            if home != path
        }
        for path, text in contents.items()
    }
    records = Parser("\n".join(contents.values())).parse()
    homes: dict[str, set[Path]] = {}
    owned = {}
    cursor = 0
    for path, text in contents.items():
        owned[path] = [record for record in records if cursor <= record.start < cursor + len(text)]
        for record in owned[path]:
            homes.setdefault(record.name, set()).add(path)
        cursor += len(text) + 1

    def complete(fields: tuple[Field, ...]) -> set[str]:
        result: set[str] = set()
        for field in fields:
            tag = re.match(r"(?:struct|union) (\w+)", field.type)
            if tag and "*" not in field.type:
                result.add(tag[1])
            result.update(complete(field.fields))
        return result

    for path, layouts in owned.items():
        for layout in layouts:
            for name in complete(layout.fields):
                dependencies[path].update(home for home in homes.get(name, set()) if home != path)
    ordered: list[Path] = []
    while dependencies:
        ready = sorted(path for path, pending in dependencies.items() if not pending)
        if not ready:
            held("shared context", "cyclic complete-type dependency: " + ", ".join(path.name for path in dependencies))
        ordered.extend(ready)
        for path in ready:
            del dependencies[path]
        for pending in dependencies.values():
            pending.difference_update(ready)
    return ordered
