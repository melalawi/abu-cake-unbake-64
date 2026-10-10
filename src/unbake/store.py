"""Lock-free content cache, append-only streams, exclusive project locks and scratch dirs."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import shutil
import tempfile
import time
import uuid
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from abucache.store import ContentCache, open_cache

from unbake import effort
from unbake.contracts import Config, Finding, Json, Refusal


def write(path: Path, content: bytes) -> bool:
    """The one way a file reaches the project: written atomically and only when its bytes differ, so a file that does
    not change keeps its mtime and nothing downstream (make, caches) sees a change. Returns whether it wrote."""
    try:
        if path.read_bytes() == content:
            return False
    except FileNotFoundError:
        pass
    path.parent.mkdir(parents=True, exist_ok=True)
    part = path.with_name(f".{uuid.uuid4().hex}.part")  # the name alone may already sit near the 255-byte limit
    part.write_bytes(content)
    part.replace(path)
    effort.forget("pin", "includes", "closure", "listing")  # the memoised reads of project files are stale now
    return True
def content(config: Config) -> ContentCache:
    """The project's content cache, in .unbake/cache, limited by the host's cache.max_bytes."""
    return open_cache(config.project.root / ".unbake" / "cache", config.host.cache_max_bytes, effort.count)
@contextmanager
def _flock(path: Path, flags: int) -> Iterator[int]:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        try:
            fcntl.flock(fd, flags | fcntl.LOCK_NB)
        except BlockingIOError:
            start = time.perf_counter()
            fcntl.flock(fd, flags)
            effort.waited(time.perf_counter() - start)
        yield fd
    finally:
        os.close(fd)  # closing releases the lock
def cached(config: Config, kind: str, key: str, produce: Callable[[], bytes]) -> bytes:
    return content(config).cached(kind, key, produce)
def get(config: Config, kind: str, key: str) -> bytes | None:
    return content(config).get(kind, key)
def put(config: Config, kind: str, key: str, value: bytes) -> None:
    content(config).put(kind, key, value)
def put_many(config: Config, kind: str, rows: Iterable[tuple[str, bytes]]) -> None:
    """Entries in one write transaction: tens of thousands of single writes queue on the cache's one writer."""
    with content(config)._cache.transact():
        for key, value in rows:
            put(config, kind, key, value)
def stem(member: str) -> str:
    """One file-name segment per member: slashes become dots, a name too long for a file keeps head and digest."""
    flat = member.replace("/", ".")
    return flat if len(flat) <= 120 else f"{flat[:80]}-{hashlib.sha256(member.encode()).hexdigest()[:16]}"
_LOG_KINDS = frozenset({"receipt", "refusal", "drain"})
_STREAM = re.compile(r"^[A-Za-z0-9_.-]+(/[A-Za-z0-9_.-]+)?$")
def _stream_path(config: Config, stream: str) -> Path:
    if not _STREAM.match(stream) or any(part in {".", ".."} for part in stream.split("/")):
        raise Refusal(Finding("store.corrupt", f"bad stream name {stream!r}"))
    return config.project.root / ".unbake" / f"{stream}.jsonl"
def listing(config: Config, folder: str) -> frozenset[str]:
    """The file names in .unbake/<folder>, read once per command: a thousand questions about files are one listing."""
    path = f"{config.project.root}/.unbake/{folder}"
    return cast(frozenset[str], effort.memo(("listing", path),
                lambda: frozenset(os.listdir(path)) if os.path.isdir(path) else frozenset()))
def append(config: Config, stream: str, row: Json) -> None:
    """Append one JSON line to .unbake/<stream>.jsonl as a single O_APPEND write under an exclusive lock."""
    path = _stream_path(config, stream)
    line = json.dumps(row, sort_keys=True, separators=(",", ":"), default=str) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    with _flock(path.with_name(path.name + ".lock"), fcntl.LOCK_EX), path.open("ab") as stream:
        stream.write(line.encode())
    effort.forget("listing")
def rows(config: Config, stream: str) -> list[Json]:
    """Read every line of .unbake/<stream>.jsonl; a line that does not parse refuses with store.corrupt."""
    path = _stream_path(config, stream)
    if not path.exists():
        return []
    result: list[Json] = []
    for number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not raw.strip():
            continue
        try:
            result.append(json.loads(raw))
        except ValueError as error:
            raise Refusal(
                Finding("store.corrupt", f"{path}:{number}: {error}", path=str(path), line=number)
            ) from error
    return result
def log(config: Config, kind: str, body: Json) -> None:
    """Append one operational ledger row (schema 2) of a known kind."""
    if kind not in _LOG_KINDS:
        raise Refusal(Finding("store.corrupt", f"invalid ledger kind {kind!r}"))
    append(
        config,
        "ledger",
        {"schema": 2, "kind": kind, "invocation": effort.invocation(),
         "time": datetime.now(UTC).isoformat(timespec="seconds"), "body": body},
    )
@contextmanager
def exclusive(config: Config, name: str) -> Iterator[bool]:
    """Try the named project lock without waiting; yield True when held, False when another process holds it."""
    path = config.project.root / ".unbake" / f"{name}.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        os.ftruncate(fd, 0)
        os.write(fd, f"{os.getpid()} {effort.invocation()}\n".encode())
        yield True
    finally:
        os.close(fd)  # the kernel releases the lock on close, exit or crash
@contextmanager
def work(config: Config) -> Iterator[Path]:
    """Yield a fresh scratch directory under the host cache and remove it on exit."""
    parent = config.project.root / ".unbake" / "tmp"
    parent.mkdir(parents=True, exist_ok=True)
    path = Path(tempfile.mkdtemp(dir=parent))
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)
