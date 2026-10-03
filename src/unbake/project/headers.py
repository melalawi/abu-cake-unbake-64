"""Discover include declarations with the same precedence as overlay compilation."""

from pathlib import Path
from typing import Any


def include_headers(project: Any) -> list[tuple[Path, str]]:
    overlays = getattr(project, "overlay_roots", ())
    fallbacks = dict(zip(project.include[len(overlays) :], overlays, strict=False)) if overlays else {}
    seen: set[Path] = set()
    headers = []
    for root in project.include:
        root = Path(root)
        for path in sorted(root.rglob("*.h")):
            relative = path.relative_to(root)
            overlay = fallbacks.get(root)
            if overlay is not None and (overlay / relative).is_file():
                continue
            resolved = path.resolve()
            if resolved not in seen:
                seen.add(resolved)
                headers.append((resolved, relative.as_posix()))
    return headers
