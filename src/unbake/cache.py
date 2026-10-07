"""The one content-keyed store: files on disk shared by every process, plus a bounded in-process memo.

Locks: produce() holds a threading.Lock only to look up or insert an in-flight Future, and an
fcntl.flock on the shard's fixed `.lock` file only around "exists? else make, then rename".
It never holds both across the make, never takes a second shard lock, and never waits on anything
but its own make. So no lock cycle can form.
"""

from __future__ import annotations

import copy
import fcntl
import functools
import hashlib
import json
import os
import pickle
import re
import stat
import sys
import tempfile
import threading
from collections import OrderedDict
from collections.abc import Callable, Hashable, Iterable, Sequence
from concurrent.futures import Future
from pathlib import Path
from typing import Any, ParamSpec, Protocol, TypeVar, cast

from unbake import atomic as atomic_files
from unbake import effort
from unbake.config import Held

T = TypeVar("T")


def key(*parts: str | bytes) -> str:
    """SHA-256 over length-framed explicit content parts; file identity belongs to inputs."""
    digest = hashlib.sha256()
    for index, part in enumerate(parts):
        if isinstance(part, str):
            part = part.encode("utf-8")
        if not isinstance(part, bytes):
            raise Held("cache", f"key part {index}: expected str or bytes")
        digest.update(len(part).to_bytes(8, "big"))
        digest.update(part)
    return digest.hexdigest()


class Codec(Protocol[T]):
    def encode(self, value: T) -> bytes: ...
    def decode(self, data: bytes) -> T: ...
    def copy(self, value: T) -> T: ...


class JsonCodec:
    def encode(self, value: Any) -> bytes:
        return serialized(value)

    def decode(self, data: bytes) -> Any:
        return json.loads(data)

    def copy(self, value: Any) -> Any:
        return copy.deepcopy(value)


class PickleCodec(JsonCodec):
    def encode(self, value: Any) -> bytes:
        return pickle.dumps(value, protocol=5)

    def decode(self, data: bytes) -> Any:
        return pickle.loads(data)


JSON = JsonCodec()
PICKLE = PickleCodec()


def clone(value: T) -> T:
    return copy.deepcopy(value)


def memory_size(value: Any) -> int:
    """Account each reachable retained Python object once, including container overhead."""
    seen: set[int] = set()

    def walk(item: Any) -> int:
        if id(item) in seen:
            return 0
        seen.add(id(item))
        total = sys.getsizeof(item)
        if isinstance(item, dict):
            total += sum(walk(k) + walk(v) for k, v in item.items())
        elif isinstance(item, (tuple, list, set, frozenset)):
            total += sum(map(walk, item))
        elif not callable(item):
            if hasattr(item, "__dict__"):
                total += walk(vars(item))
            for base in type(item).__mro__:
                slots = getattr(base, "__slots__", ())
                for slot in (slots,) if isinstance(slots, str) else slots:
                    if slot not in ("__dict__", "__weakref__") and hasattr(item, slot):
                        total += walk(getattr(item, slot))
        return total

    return walk(value)


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

    def _find(self, kind: str, content_key: str) -> Path | None:
        path = self.path(kind, content_key)
        try:
            mode = path.stat().st_mode
        except FileNotFoundError:
            return None
        if stat.S_ISREG(mode):
            return path
        raise Held("cache", f"{path}: expected cached file")

    def get(self, kind: str, content_key: str) -> Path | None:
        path = self._find(kind, content_key)
        effort.count("cache." + kind, int(path is not None), 1)
        return path

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
        cached = self._find(kind, content_key)
        if cached is not None:
            effort.count("cache." + kind, 1, 1)
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
            effort.count("cache." + kind, 1, 1)
            return running.result()
        try:
            result, made = self._produce_locked(kind, content_key, make)
            effort.count("cache." + kind, int(not made), 1)
        except BaseException as error:
            running.set_exception(error)
            raise
        else:
            running.set_result(result)
            return result
        finally:
            with _inflight_lock:
                _inflight.pop(identity, None)

    def value(self, kind: str, content_key: str, codec: Codec[T], compute: Callable[[], T]) -> T:
        path = self.produce(kind, content_key, lambda target: atomic_files.fresh(target, codec.encode(compute())))
        return self.decode(path, codec)

    def decode(self, path: Path, codec: Codec[T]) -> T:
        from unbake import inputs

        content = inputs.digest(path, algorithm="sha256", reuse=configured())

        def read() -> T:
            try:
                return codec.decode(path.read_bytes())
            except (OSError, ValueError, TypeError, pickle.UnpicklingError, EOFError, AttributeError) as error:
                raise Held("cache", f"cache.corrupt: {path}: {error}") from error

        return memo(
            "decode",
            (type(codec).__module__, type(codec).__qualname__, content),
            read,
            size=memory_size,
            copy_out=codec.copy,
        )

    def certificates(self, kind: str, environment_key: str) -> CertificateSet:
        return CertificateSet(self, kind, environment_key)

    def _produce_locked(self, kind: str, content_key: str, make: Callable[[Path], None]) -> tuple[Path, bool]:
        """The entry and whether this call ran make."""
        path = self.path(kind, content_key)
        path.parent.mkdir(parents=True, exist_ok=True)
        with (path.parent / ".lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            cached = self._find(kind, content_key)
            if cached is not None:
                return cached, False
            descriptor, name = tempfile.mkstemp(prefix=".pending-", dir=path.parent)
            os.close(descriptor)
            temporary = Path(name)
            try:
                temporary.unlink()
                make(temporary)
                if not temporary.is_file():
                    raise Held("cache", f"make output {temporary}: expected file")
                os.replace(temporary, path)
                return path, True
            except OSError as error:
                raise Held("cache", f"{path}: {error}") from error
            finally:
                temporary.unlink(missing_ok=True)


def entries(root: Path) -> list[Path]:
    """Every cached file under root, excluding shard lock files and pending temporaries."""
    root = Path(root)
    if not root.is_dir():
        return []
    result: list[Path] = []
    for path in root.glob("*/*/*"):
        if path.is_dir() and path.name.endswith(".certificates"):
            result.extend(path.glob("*.json"))
        elif path.is_file() and path.name != ".lock" and not path.name.startswith(".pending-"):
            result.append(path)
    return result


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


_memo: OrderedDict[tuple[str, Hashable], tuple[Any, int]] = OrderedDict()
_memo_lock = threading.Lock()
_memo_flights: dict[tuple[str, Hashable], Future[Any]] = {}
_budget: int | None = None
_resident = 0


def configure(*, memory_bytes: int) -> None:
    global _budget
    if type(memory_bytes) is not int or memory_bytes <= 0:
        raise Held("cache", "cache.memory_bytes: required positive integer")
    with _memo_lock:
        _budget = memory_bytes
        _evict()


def configured() -> bool:
    return _budget is not None


def resident_bytes() -> int:
    with _memo_lock:
        return _resident


def _evict() -> None:
    global _resident
    while _memo and (_budget is None or _resident > _budget):
        _, (_, weight) = _memo.popitem(last=False)
        _resident -= weight


def remember(
    kind: str, content: Hashable, value: T, *, size: Callable[[Any], int], copy_out: Callable[[Any], Any]
) -> T:
    global _resident
    identity = (kind, content)
    value_bytes = size(value)
    if type(value_bytes) is not int or value_bytes < 0:
        raise Held("cache", "cache.size: required nonnegative integer bytes")
    weight = value_bytes + memory_size(identity)
    if weight < 0:
        raise Held("cache", "cache.size: required nonnegative integer bytes")
    with _memo_lock:
        old = _memo.pop(identity, None)
        if old is not None:
            _resident -= old[1]
        if _budget is not None and weight <= _budget:
            _memo[identity] = (copy_out(value), weight)
            _resident += weight
        _evict()
    return value


def memo(
    kind: str,
    content: Hashable,
    compute: Callable[[], T],
    *,
    size: Callable[[Any], int],
    copy_out: Callable[[Any], Any],
) -> T:
    """One producer per retained computation, one process budget, caller-owned output."""
    identity = (kind, content)
    with _memo_lock:
        found = _memo.get(identity)
        if found is not None:
            _memo.move_to_end(identity)
            return cast(T, copy_out(found[0]))
        running = _memo_flights.get(identity)
        owner = running is None
        if owner:
            running = Future()
            _memo_flights[identity] = running
    assert running is not None
    if not owner:
        return cast(T, copy_out(running.result()))
    try:
        value = compute()
        result = remember(kind, content, value, size=size, copy_out=copy_out)
        running.set_result(copy_out(value))
        return result
    except BaseException as error:
        running.set_exception(error)
        raise
    finally:
        with _memo_lock:
            _memo_flights.pop(identity, None)


P = ParamSpec("P")


def memoized(
    kind: str, *, size: Callable[[Any], int], copy_out: Callable[[Any], Any]
) -> Callable[[Callable[P, T]], Callable[P, T]]:
    def decorate(function: Callable[P, T]) -> Callable[P, T]:
        @functools.wraps(function)
        def call(*args: P.args, **kwargs: P.kwargs) -> T:
            return memo(
                kind,
                (args, tuple(sorted(kwargs.items()))),
                lambda: function(*args, **kwargs),
                size=size,
                copy_out=copy_out,
            )

        return call

    return decorate


def retained(kind: str) -> tuple[tuple[Hashable, Any], ...]:
    with _memo_lock:
        return tuple((content, copy.deepcopy(value)) for (name, content), (value, _) in _memo.items() if name == kind)


def parsed(kind: str, paths: Path | Sequence[Path], parse: Callable[[], T], *, extra: Hashable = None) -> T:
    from unbake import inputs

    files = (Path(paths),) if isinstance(paths, (str, Path)) else tuple(Path(path) for path in paths)
    try:
        pins = tuple((str(path), inputs.digest(path, algorithm="sha256", reuse=configured())) for path in files)
    except OSError:
        return parse()
    return memo("parsed." + kind, (pins, extra), parse, size=memory_size, copy_out=clone)


def forget(kinds: Sequence[str] | None = None) -> None:
    global _resident
    with _memo_lock:
        for identity in list(_memo):
            if kinds is None or identity[0] in kinds:
                _resident -= _memo.pop(identity)[1]


class CertificateSet:
    """Immutable certificate batches use the same Cache, with no per-declaration writes."""

    def __init__(self, cache: Cache, kind: str, environment_key: str) -> None:
        self.cache = cache
        self.directory = cache.path(kind, environment_key).with_suffix(".certificates")

    def contains(self, keys: Iterable[str]) -> frozenset[str]:
        from unbake import inputs

        files = tuple(sorted(self.directory.glob("*.json")))
        pins = tuple((str(path), inputs.digest(path, algorithm="sha256", reuse=configured())) for path in files)

        def load() -> frozenset[str]:
            known = set()
            for path in files:
                value = self.cache.decode(path, JSON)
                if not isinstance(value, list) or any(
                    not isinstance(k, str) or not re.fullmatch(r"[0-9a-f]{64}", k) for k in value
                ):
                    raise Held("cache", f"cache.certificates: corrupt batch {path}")
                known.update(value)
            return frozenset(known)

        known = memo("certificates", pins, load, size=memory_size, copy_out=frozenset)
        return frozenset(known.intersection(keys))

    def add(self, keys: Iterable[str]) -> None:
        wanted = sorted(set(keys))
        if not wanted:
            return
        content = JSON.encode(wanted)
        self.directory.mkdir(parents=True, exist_ok=True)
        atomic_files.write(self.directory / (key(content) + ".json"), content, durable=False)


def serialized(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
