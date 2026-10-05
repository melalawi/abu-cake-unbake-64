"""Per-source declaration facts, cached by content key.

A published source's facts depend only on its bytes, its include closure, the
preprocessor command and SCHEMA. Each (source, version) pair is extracted once
and then read back from the shared cache. Large shared values (struct templates,
typedef maps) are stored once by digest and interned on load, so many sources
that include the same headers share one object.

A worker takes every task of a source together. Versions whose preprocessed unit
is the same text are extracted once and stamped with each task's provenance.
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
# Bump when the facts extract() produces change for the same inputs. Keys never digest the tool's code.
SCHEMA = 1
_INCLUDE = re.compile(r'^[ \t]*#[ \t]*include[ \t]*([<"])([^>"\n]+)[>"]', re.M)
# At most this many sources per worker job: neighbours in include order share their header expansion.
SOURCES_PER_JOB = 24
# At least this many jobs per worker, so a small fill still spreads over every worker.
JOBS_PER_WORKER = 4
# Placeholders for the provenance fields that differ between tasks sharing one unit text.
_FUNCTION = "\x00function"
_VERSION = "\x00version"

Task = tuple[str, Path, str]


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
        self._generated: frozenset[Path] | None = None

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

    def generated(self) -> frozenset[Path]:
        if self._generated is None:
            from unbake.layout import index

            self._generated = frozenset(index.headers(self.project))
        return self._generated

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
    parts: list[str | bytes] = [FACTS, str(SCHEMA), "source", version, json.dumps(command), function]
    parts.append(storage.relative(project, source))
    parts.append(source.read_bytes())
    # Generated headers are written from the type solution these facts feed: keying on their bytes made every
    # publish invalidate every source's facts and run the solve again. Their names stay in the key.
    generated = snapshot.generated()
    for path in snapshot.closure(roots):
        parts.extend((storage.relative(project, path), "generated" if path in generated else snapshot.digest(path)))
    return key(*parts)


def text_key(text: str, provenance: dict[str, Any], authored: list[str]) -> str:
    return key(FACTS, str(SCHEMA), "text", text, json.dumps(provenance, sort_keys=True), "\n".join(authored))


def _write(path: Path, *, data: bytes) -> None:
    """Cache.produce renames the private path into place; an entry is re-derivable, so no fsync."""
    atomic_files.fresh(path, data)


def _load(path: Path) -> Any:
    try:
        return json.loads(path.read_bytes())
    except (OSError, ValueError) as error:
        raise Held("solve", f"facts.cache: unreadable entry {path}: {error}; delete it and rerun") from error


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
            value = _load(path)
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

    def encoded(self, seeds: list[dict[str, Any]]) -> bytes:
        return json.dumps([self.encode(seed) for seed in seeds], separators=(",", ":")).encode()

    def put(self, content_key: str, seeds: list[dict[str, Any]]) -> None:
        self.put_encoded(content_key, self.encoded(seeds))

    def put_encoded(self, content_key: str, data: bytes) -> None:
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
            rows = None if path is None else _load(path)
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


def _provenance(project: Project, function: str, version: str, source: Path) -> dict[str, Any]:
    return {
        "kind": "published",
        "function": function,
        "version": version,
        "source": storage.relative(project, source),
        "sha256": inputs.digest(source),
    }


def _unit_seeds(text: str, provenance: dict[str, Any], source: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    """The contracts a unit consumes and every definition it owns (all functions)."""
    contract = declarations.published(text, provenance, source, contracts=True, compact=True)
    consumed = declarations.consumed_contracts(contract, source.read_text())
    # The definition owns the function contract; imported prototypes are
    # dependencies, and cannot override a ROM-proven definition elsewhere.
    definition = declarations.published(text, {**provenance, "kind": "proven"}, source, compact=True)
    return consumed, definition


def _owned(definition: dict[str, Any], function: str) -> dict[str, Any]:
    return {**definition, "functions": {name: row for name, row in definition["functions"].items() if name == function}}


def extract(project: Project, policy: Host | None, task: Task) -> list[dict[str, Any]]:
    """Two seeds: the contracts the source consumes, and the definition it owns."""
    function, source, version = task
    text = declarations.source_unit(project, policy, version, source)
    consumed, definition = _unit_seeds(text, _provenance(project, function, version, source), source)
    return [consumed, _owned(definition, function)]


def _stamp(data: bytes, function: str, version: str) -> bytes:
    for placeholder, value in ((_FUNCTION, function), (_VERSION, version)):
        data = data.replace(json.dumps(placeholder).encode(), json.dumps(value).encode())
    return data


def source_facts(project: Project, policy: Host | None, output: Store, tasks: list[Task]) -> list[bytes]:
    """Encoded facts of every task of one source: each distinct unit text is extracted once."""
    source = tasks[0][1]
    placeholder = _provenance(project, _FUNCTION, _VERSION, source)
    units: dict[str, tuple[bytes, dict[str, Any]]] = {}
    texts = {version: declarations.source_unit(project, policy, version, source) for version in {t[2] for t in tasks}}
    result = []
    for function, task_source, version in tasks:
        if task_source != source:
            raise Held("solve", f"facts.source: {task_source} grouped with {source}")
        text = texts[version]
        if text not in units:
            try:
                consumed, definition = _unit_seeds(text, placeholder, source)
            except Held:
                # A refusal names the task's own provenance, exactly as a single extraction does.
                extract(project, policy, (function, source, version))
                raise
            units[text] = output.encoded([consumed]), definition
        consumed_data, definition = units[text]
        owned = output.encoded([_owned(definition, function)])
        result.append(_stamp(consumed_data[:-1] + b"," + owned[1:], function, version))
    return result


def _check_keys(project: Project, host: Host | None, group: list[tuple[str, Task]]) -> None:
    """The inputs a key was computed from are the inputs extracted: a source edited mid-solve is refused."""
    snapshot = Snapshot(project)
    for content_key, task in group:
        if source_key(project, host, task, snapshot) != content_key:
            raise Held("solve", f"facts.inputs: {storage.relative(project, task[1])} changed during the solve; rerun")


def _fill(project: Project, host: Host | None, output: Store, group: list[tuple[str, Task]]) -> None:
    _check_keys(project, host, group)
    encoded = source_facts(project, host, output, [task for _, task in group])
    _check_keys(project, host, group)
    for (content_key, _), data in zip(group, encoded, strict=True):
        output.put_encoded(content_key, data)


def _job(job: tuple[Project, Host, list[list[tuple[str, Task]]]]) -> None:
    """Worker body: extract each source's missing tasks straight into the shared cache."""
    project, host, sources = job
    output = store(host)
    for group in sources:
        _fill(project, host, output, group)


def _include_order(source: Path) -> tuple[tuple[str, ...], str]:
    """Sources whose include lines match sit together, so their header expansions are shared."""
    return tuple(name for _, name in _includes(source, inputs.signature(source))), str(source)


def compute(project: Project, host: Host | None, output: Store, misses: list[tuple[str, Task]]) -> None:
    """Fill the store for missing tasks; worker processes share the cache on disk."""
    groups: dict[Path, list[tuple[str, Task]]] = {}
    for miss in sorted(misses, key=lambda miss: (miss[1][2], miss[1][0])):
        groups.setdefault(miss[1][1], []).append(miss)
    ordered = [groups[source] for source in sorted(groups, key=_include_order)]
    if output.cache is None or host is None:
        for group in ordered:
            _fill(project, host, output, group)
        return
    from unbake import pool

    workers = pool.Pool.from_host(host).size
    size = max(1, min(SOURCES_PER_JOB, -(-len(ordered) // (workers * JOBS_PER_WORKER))))
    jobs = [(project, host, ordered[start : start + size]) for start in range(0, len(ordered), size)]
    pool.run(host, _job, jobs)


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
