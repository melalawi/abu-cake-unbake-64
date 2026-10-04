"""Saved drafts reach the coordinator as ("edit", path) messages: inotify via watchfiles, debounced, no polling."""

from __future__ import annotations

import queue
import threading
from pathlib import Path
from typing import Any


def wanted(path: Path, work: Path) -> bool:
    """build/work/FUNC/FUNC.c only (not include/, not attempts or objects)."""
    try:
        relative = path.relative_to(work)
    except ValueError:
        return False
    return len(relative.parts) == 2 and relative.suffix == ".c" and relative.stem == relative.parts[0]


def watch(work: Path, debounce_ms: int, stop: threading.Event, inbox: queue.Queue[tuple[str, Any]]) -> None:
    import watchfiles

    work.mkdir(parents=True, exist_ok=True)
    for changes in watchfiles.watch(work, debounce=debounce_ms, stop_event=stop, force_polling=False, recursive=True):
        for _change, name in sorted(changes):
            path = Path(name)
            if wanted(path, work):
                inbox.put(("edit", str(path)))
