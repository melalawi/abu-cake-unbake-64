"""The one content-keyed store: files on disk shared by every process, plus a bounded in-process memo.

Locks: produce() holds a threading.Lock only to look up or insert an in-flight Future, and an
fcntl.flock on the shard's fixed `.lock` file only around "exists? else make, then rename".
It never holds both across the make, never takes a second shard lock, and never waits on anything
but its own make. So no lock cycle can form.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import stat
import tempfile
import threading
from collections import OrderedDict
from collections.abc import Callable, Hashable, Sequence
from concurrent.futures import Future
from pathlib import Path
from typing import Any, TypeVar

from unbake import atomic as atomic_files
from unbake.config import Held

T = TypeVar("T")
MEMO_ENTRIES_PER_KIND = 64


def key(*parts: str | bytes | Path) -> str:
    """SHA-256 over length-framed parts; a Path part contributes its file bytes."""
    digest = hashlib.sha256()
    for index, part in enumerate(parts):
        try:
            if isinstance(part, Path):
                with part.open("rb") as source:
                    digest.update(os.fstat(source.fileno()).st_size.to_bytes(8, "big"))
                    while block := source.read(1024 * 1024):
                        digest.update(block)
                continue
            if isinstance(part, str):
                part = part.encode("utf-8")
            if not isinstance(part, bytes):
                raise Held("cache", f"key part {index}: expected str, bytes or Path")
            digest.update(len(part).to_bytes(8, "big"))
            digest.update(part)
        except OSError as error:
            raise Held("cache", f"key part {index} {part!s}: {error}") from error
    return digest.hexdigest()


_inflight: dict[tuple[str, str], Future[Path]] = {}
_inflight_lock = threading.Lock()


class Cache:
    def __init__(self, root: Path) -> None:
        self.root = Path(root).expanduser().absolute()

    def path(self, kind: str, content_key: str) -> Path:
        if not isinstance(kind, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", kind):
            raise Held("cache", f"kind {kind!r}: expected a single cache kind")
        if not isinstance(content_key, str) or not re.fullmatch(r"[0-9a-f]{64}", content_key):
            raise Held("cache", f"key {content_key!r}: expected SHA-256")
        return self.root / kind / content_key[:2] / content_key

    def get(self, kind: str, content_key: str) -> Path | None:
        path = self.path(kind, content_key)
        try:
            mode = path.stat().st_mode
        except FileNotFoundError:
            return None
        if stat.S_ISREG(mode):
            return path
        raise Held("cache", f"{path}: expected cached file")

    def put(self, kind: str, content_key: str, src: Path) -> Path:
        path = self.path(kind, content_key)
        try:
            if not src.is_file():
                raise Held("cache", f"src {src}: expected file")
            path.parent.mkdir(parents=True, exist_ok=True)
            atomic_files.copyfile(src, path, durable=False)
            return path
        except OSError as error:
            raise Held("cache", f"{path} from src {src}: {error}") from error

    def produce(self, kind: str, content_key: str, make: Callable[[Path], None]) -> Path:
        """Return the entry, computing it at most once across threads and processes."""
        cached = self.get(kind, content_key)
        if cached is not None:
            return cached
        identity = (str(self.root), kind + "/" + content_key)
        with _inflight_lock:
            running = _inflight.get(identity)
            owner = running is None
            if owner:
                running = Future()
                _inflight[identity] = running
        assert running is not None
        if not owner:
            return running.result()
        try:
            result = self._produce_locked(kind, content_key, make)
        except BaseException as error:
            running.set_exception(error)
            raise
        else:
            running.set_result(result)
            return result
        finally:
            with _inflight_lock:
                _inflight.pop(identity, None)

    def _produce_locked(self, kind: str, content_key: str, make: Callable[[Path], None]) -> Path:
        path = self.path(kind, content_key)
        path.parent.mkdir(parents=True, exist_ok=True)
        with (path.parent / ".lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            cached = self.get(kind, content_key)
            if cached is not None:
                return cached
            descriptor, name = tempfile.mkstemp(prefix=".pending-", dir=path.parent)
            os.close(descriptor)
            temporary = Path(name)
            try:
                temporary.unlink()
                make(temporary)
                if not temporary.is_file():
                    raise Held("cache", f"make output {temporary}: expected file")
                os.replace(temporary, path)
                return path
            except OSError as error:
                raise Held("cache", f"{path}: {error}") from error
            finally:
                temporary.unlink(missing_ok=True)


def entries(root: Path) -> list[Path]:
    """Every cached file under root, excluding shard lock files and pending temporaries."""
    root = Path(root)
    if not root.is_dir():
        return []
    return [
        path
        for path in root.glob("*/*/*")
        if path.is_file() and path.name != ".lock" and not path.name.startswith(".pending-")
    ]


def trim(root: Path, max_bytes: int, trim_to_bytes: int) -> list[Path]:
    """Delete least-recently-used entries once the total passes max_bytes, down to trim_to_bytes."""
    if trim_to_bytes >= max_bytes:
        raise Held("cache", "trim: trim_to_bytes must be less than max_bytes")
    rows = []
    for path in entries(root):
        info = path.stat()
        rows.append((info.st_atime_ns, info.st_mtime_ns, str(path), info.st_size, path))
    total = sum(row[3] for row in rows)
    removed: list[Path] = []
    if total <= max_bytes:
        return removed
    for _, _, _, size, path in sorted(rows):
        if total <= trim_to_bytes:
            break
        path.unlink(missing_ok=True)
        total -= size
        removed.append(path)
    return removed


_memo: dict[str, OrderedDict[Hashable, Any]] = {}
# A cycle refreshes steps on a second thread; lookups and evictions are one step each, compute runs unlocked.
_memo_lock = threading.Lock()


def memo(kind: str, content: Hashable, compute: Callable[[], T], *, keep: int = MEMO_ENTRIES_PER_KIND) -> T:
    """In-process reuse of a value derived from content; each kind keeps its latest `keep` values."""
    with _memo_lock:
        values = _memo.setdefault(kind, OrderedDict())
        if content in values:
            values.move_to_end(content)
            return values[content]  # type: ignore[no-any-return]
    value = compute()
    with _memo_lock:
        values[content] = value
        while len(values) > keep:
            values.popitem(last=False)
    return value


def parsed(kind: str, paths: Path | Sequence[Path], parse: Callable[[], T], *, extra: Hashable = None) -> T:
    """Parse project files once per process while their bytes are unchanged."""
    from unbake import inputs

    files = (Path(paths),) if isinstance(paths, (str, Path)) else tuple(Path(path) for path in paths)
    try:
        digests = tuple(inputs.digest(path) for path in files)
    except OSError:
        return parse()
    return memo("parsed." + kind, (tuple(map(str, files)), digests, extra), parse)


def forget(kinds: Sequence[str] | None = None) -> None:
    """Drop in-process memo entries (all kinds when None)."""
    with _memo_lock:
        for kind in list(_memo) if kinds is None else kinds:
            _memo.pop(kind, None)


def serialized(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
