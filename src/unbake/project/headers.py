"""Discover include declarations with the same precedence as overlay compilation."""

import os
from collections.abc import Callable
from pathlib import Path
from typing import Any


def include_headers(project: Any, *, exclude: Callable[[Path], bool] | None = None) -> list[tuple[Path, str]]:
    overlays = getattr(project, "overlay_roots", ())
    fallbacks = dict(zip(project.include[len(overlays) :], overlays, strict=False)) if overlays else {}
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
            relative = path.relative_to(root)
            overlay = fallbacks.get(root)
            if overlay is not None and (overlay / relative).is_file():
                continue
            resolved = path.resolve()
            if resolved not in seen:
                seen.add(resolved)
                headers.append((resolved, relative.as_posix()))
    return headers
