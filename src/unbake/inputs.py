"""One file-digest table per process, keyed by stat signature."""

from __future__ import annotations

import hashlib
from pathlib import Path

Signature = tuple[int, int, int, int, int]
_digests: dict[Path, tuple[Signature, str]] = {}


def signature(path: Path) -> Signature:
    info = path.stat()
    return info.st_dev, info.st_ino, info.st_mtime_ns, info.st_ctime_ns, info.st_size


def digest(path: Path) -> str:
    """SHA-256 of the file's bytes, recomputed only when its stat signature changes."""
    path = Path(path)
    current = signature(path)
    cached = _digests.get(path)
    if cached is not None and cached[0] == current:
        return cached[1]
    hasher = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            hasher.update(block)
    result = hasher.hexdigest()
    _digests[path] = current, result
    return result
