"""List the headers a unit can include, with a draft's private copies shadowing include/."""

import os
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any


def include_closure(contents: dict[Path, str], roots: tuple[Path, ...], selected: set[Path]) -> set[Path]:
    """Retain installed providers explicitly included by selected headers.

    Resolve quoted imports beside their includer first, then in include-root
    order; angle imports use the roots. Only the effective header view can
    supply a provider, so private copies still shadow public headers.
    """
    selected = set(selected)
    pending = list(selected)
    while pending:
        parent = pending.pop()
        text = re.sub(
            r'"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'|/\*.*?\*/|//[^\n]*',
            lambda match: re.sub(r"[^\n]", " ", match[0]) if match[0].startswith(("/*", "//")) else match[0],
            contents[parent],
            flags=re.S,
        )
        for match in re.finditer(r'^[ \t]*#[ \t]*include[ \t]*([<"])([^>"\n]+)[>"]', text, re.M):
            homes = (parent.parent, *roots) if match[1] == '"' else roots
            for home in homes:
                path = (home / match[2]).resolve()
                if path in contents:
                    if path not in selected:
                        selected.add(path)
                        pending.append(path)
                    break
    return selected


def include_headers(project: Any, *, exclude: Callable[[Path], bool] | None = None) -> list[tuple[Path, str]]:
    """(path, relative name) of every header; a file in a work include root hides the same name below it."""
    work_roots = tuple(getattr(project, "work_include", ()))
    seen_names: set[str] = set()
    seen: set[Path] = set()
    headers = []
    for root in project.include:
        root = Path(root)
        if exclude is None:
            paths = sorted(root.rglob("*.h"))
        else:
            paths = []
            for directory, folders, files in os.walk(root):
                parent = Path(directory)
                folders[:] = [name for name in folders if not exclude(parent / name)]
                paths.extend(parent / name for name in files if name.endswith(".h") and not exclude(parent / name))
            paths.sort()
        for path in paths:
            relative = path.relative_to(root).as_posix()
            if root not in work_roots and relative in seen_names:
                continue
            resolved = path.resolve()
            if resolved not in seen:
                seen.add(resolved)
                seen_names.add(relative)
                headers.append((resolved, relative))
    return headers
