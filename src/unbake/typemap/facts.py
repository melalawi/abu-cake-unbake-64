"""Per-source declaration facts, cached by content key.

A published source's facts depend only on its bytes, its include closure, the
preprocessor command and the code that extracts them. Each (source, version)
pair is extracted once and then read back from the shared cache. Large shared
values (struct templates, typedef maps) are stored once by digest and interned
on load, so many sources that include the same headers share one object.
"""

from __future__ import annotations

import functools
import hashlib
import itertools
import json
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

from unbake import atomic as atomic_files
from unbake import inputs
from unbake.cache import Cache, key
from unbake.config import Held, Host, Project
from unbake.typemap import declarations, storage

FACTS = "facts"
SHARED = "facts-shared"
_PRODUCERS = (
    "typemap/declarations.py",
    "typemap/facts.py",
    "cdecl.py",
    "decomp/draft_context.py",
)
_INCLUDE = re.compile(r'^[ \t]*#[ \t]*include[ \t]*([<"])([^>"\n]+)[>"]', re.M)
_CHUNK = 16

Task = tuple[str, Path, str]


@functools.cache
def fingerprint() -> str:
    """Digest of the modules that compute facts; a code change invalidates only this kind."""
    root = Path(__file__).resolve().parent.parent
    return key(*(root / name for name in _PRODUCERS))


@functools.cache
def _includes(path: Path, stamp: tuple[int, int, int, int, int]) -> tuple[tuple[str, str], ...]:
    return tuple((match[1], match[2]) for match in _INCLUDE.finditer(path.read_text(errors="replace")))


class Snapshot:
    """Include edges, closures and digests read once per key computation; files do not change during one."""

    def __init__(self, project: Project) -> None:
        self.project = project
        self._edges: dict[Path, tuple[Path, ...]] = {}
        self._closures: dict[tuple[Path, ...], tuple[Path, ...]] = {}
        self._digests: dict[Path, str] = {}

    def edges(self, path: Path) -> tuple[Path, ...]:
        found = self._edges.get(path)
        if found is None:
            resolved = []
            for quote, name in _includes(path, inputs.signature(path)):
                places = ([path.parent] if quote == '"' else []) + list(self.project.include)
                target = next((place / name for place in places if (place / name).is_file()), None)
                if target is not None:
                    resolved.append(target)
            found = self._edges[path] = tuple(resolved)
        return found

    def closure(self, roots: tuple[Path, ...]) -> tuple[Path, ...]:
        """Every file the roots can include, ignoring conditionals (a superset is safe)."""
        found = self._closures.get(roots)
        if found is None:
            seen: dict[Path, None] = {}
            pending = list(roots)
            while pending:
                for target in self.edges(pending.pop()):
                    if target not in seen:
                        seen[target] = None
                        pending.append(target)
            found = self._closures[roots] = tuple(sorted(seen))
        return found

    def digest(self, path: Path) -> str:
        found = self._digests.get(path)
        if found is None:
            found = self._digests[path] = inputs.digest(path)
        return found


def _forced(project: Project, command: list[str]) -> list[Path]:
    forced = [project.root / value for flag, value in itertools.pairwise(command) if flag == "-include"]
    return [path for path in forced if path.is_file()]


def _command(project: Project, policy: Host | None, version: str) -> list[str]:
    if policy is None:
        return ["in-memory", version, *project.version(version).macros]
    command = declarations._cpp_command(project, policy, version, extra=True, line_markers=False)
    return [part.replace(str(project.root), ".") for part in command]


def source_key(project: Project, policy: Host | None, task: Task, snapshot: Snapshot) -> str:
    function, source, version = task
    command = _command(project, policy, version)
    roots = (source, *_forced(project, command))
    parts: list[str | bytes] = [fingerprint(), "source", version, json.dumps(command), function]
    parts.append(storage.relative(project, source))
    parts.append(source.read_bytes())
    for path in snapshot.closure(roots):
        parts.extend((storage.relative(project, path), snapshot.digest(path)))
    return key(*parts)


def text_key(text: str, provenance: dict[str, Any], authored: list[str]) -> str:
    return key(fingerprint(), "text", text, json.dumps(provenance, sort_keys=True), "\n".join(authored))


def _write(path: Path, *, data: bytes) -> None:
    atomic_files.write(path, data)


class Store:
    """Encode and intern seeds in the shared cache."""

    def __init__(self, cache: Cache | None) -> None:
        self.cache = cache
        self.memory: dict[str, Any] = {}
        self.shared: dict[str, Any] = {}
        self.written: dict[int, tuple[Any, str]] = {}

    def _put_shared(self, value: Any) -> str:
        known = self.written.get(id(value))
        if known is not None and known[0] is value:
            return known[1]
        data = json.dumps(value, separators=(",", ":")).encode()
        digest = hashlib.sha256(data).hexdigest()
        self.written[id(value)] = value, digest
        if digest not in self.shared:
            self.shared[digest] = json.loads(data)
            if self.cache is not None:
                self.cache.produce(SHARED, digest, functools.partial(_write, data=data))
        return digest

    def _get_shared(self, digest: str) -> Any:
        value = self.shared.get(digest)
        if value is None:
            if self.cache is None or (path := self.cache.get(SHARED, digest)) is None:
                raise Held("solve", f"facts.shared: missing {digest}")
            value = json.loads(path.read_bytes())
            self.shared[digest] = value
        return value

    def encode(self, seed: dict[str, Any]) -> dict[str, Any]:
        result = {}
        for name, value in seed.items():
            if name == "structs" and isinstance(value, declarations.ProvenStructs):
                result[name] = {"$template": self._put_shared(value.template), "provenance": value.provenance}
            elif name in ("aliases", "shared_typedefs"):
                result[name] = {"$shared": self._put_shared(value)}
            else:
                result[name] = value
        return result

    def decode(self, seed: dict[str, Any]) -> dict[str, Any]:
        result = dict(seed)
        structs = result.get("structs")
        if isinstance(structs, dict) and "$template" in structs:
            result["structs"] = declarations.ProvenStructs(
                self._get_shared(structs["$template"]), structs["provenance"]
            )
        for name in ("aliases", "shared_typedefs"):
            if name in result:
                result[name] = self._get_shared(result[name]["$shared"])
        return result

    def put(self, content_key: str, seeds: list[dict[str, Any]]) -> None:
        data = json.dumps([self.encode(seed) for seed in seeds], separators=(",", ":")).encode()
        if self.cache is None:
            self.memory[content_key] = json.loads(data)
        else:
            self.cache.produce(FACTS, content_key, functools.partial(_write, data=data))

    def has(self, content_key: str) -> bool:
        if self.cache is None:
            return content_key in self.memory
        return self.cache.get(FACTS, content_key) is not None

    def get(self, content_key: str) -> list[dict[str, Any]] | None:
        if self.cache is None:
            rows = self.memory.get(content_key)
        else:
            path = self.cache.get(FACTS, content_key)
            rows = None if path is None else json.loads(path.read_bytes())
        return None if rows is None else [self.decode(row) for row in rows]

    def text(
        self, text: str, provenance: dict[str, Any], authored: set[Path], compute: Callable[[], dict[str, Any]]
    ) -> dict[str, Any]:
        content_key = text_key(text, provenance, sorted(str(path) for path in authored))
        rows = self.get(content_key)
        if rows is None:
            self.put(content_key, [compute()])
            rows = self.get(content_key)
            assert rows is not None
        return rows[0]


def store(policy: Host | None) -> Store:
    return Store(None if policy is None else Cache(policy.cache_root))


def extract(project: Project, policy: Host | None, task: Task) -> list[dict[str, Any]]:
    """Two seeds: the contracts the source consumes, and the definition it owns."""
    function, source, version = task
    text = declarations.source_unit(project, policy, version, source)
    provenance = {
        "kind": "published",
        "function": function,
        "version": version,
        "source": storage.relative(project, source),
        "sha256": inputs.digest(source),
    }
    contract = declarations.published(text, provenance, source, contracts=True, compact=True)
    consumed = declarations.consumed_contracts(contract, source.read_text())
    # The definition owns the function contract; imported prototypes are
    # dependencies, and cannot override a ROM-proven definition elsewhere.
    definition = declarations.published(text, {**provenance, "kind": "proven"}, source, compact=True)
    definition["functions"] = {name: row for name, row in definition["functions"].items() if name == function}
    return [consumed, definition]


def _chunk(job: tuple[Project, Host, list[tuple[str, Task]]]) -> None:
    """Worker body: extract misses straight into the shared cache."""
    project, host, tasks = job
    output = store(host)
    for content_key, task in tasks:
        output.put(content_key, extract(project, host, task))


def compute(project: Project, host: Host | None, output: Store, misses: list[tuple[str, Task]]) -> None:
    """Fill the store for missing tasks; worker processes share the cache on disk."""
    if output.cache is None or host is None:
        for content_key, task in misses:
            output.put(content_key, extract(project, host, task))
        return
    from unbake import pool

    # One version's sources in a row share their header expansion, which cdecl.resumable_parse parses once.
    misses = sorted(misses, key=lambda miss: (miss[1][2], str(miss[1][1]), miss[1][0]))
    chunks = [(project, host, misses[start : start + _CHUNK]) for start in range(0, len(misses), _CHUNK)]
    pool.run(host, _chunk, chunks)


def published_keys(project: Project, policy: Host | None) -> list[str]:
    snapshot = Snapshot(project)
    return [source_key(project, policy, task, snapshot) for task in declarations.published_sources(project)]


def published(project: Project, policy: Host | None, output: Store, keys: list[str]) -> list[dict[str, Any]]:
    """Seeds of every published source in inventory order; keys come from published_keys."""
    tasks = declarations.published_sources(project)
    if len(tasks) != len(keys):
        raise Held("solve", "facts.keys: published sources changed during the solve")
    misses = [(content_key, task) for content_key, task in zip(keys, tasks, strict=True) if not output.has(content_key)]
    compute(project, policy, output, misses)
    seeds: list[dict[str, Any]] = []
    for content_key in keys:
        rows = output.get(content_key)
        if rows is None:
            raise Held("solve", f"facts.{content_key}: missing after extraction")
        seeds.extend(rows)
    return seeds


def refresh(project: Project, policy: Host, tasks: list[Task]) -> dict[str, str]:
    """Extract facts for these sources now and return the refusal reason of each failing function."""
    output = store(policy)
    refused: dict[str, str] = {}
    snapshot = Snapshot(project)
    for task in tasks:
        content_key = source_key(project, policy, task, snapshot)
        if task[0] in refused or output.has(content_key):
            continue
        try:
            output.put(content_key, extract(project, policy, task))
        except Held as error:
            refused[task[0]] = error.reason
    return refused
