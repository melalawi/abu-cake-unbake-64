"""Per-source declaration facts: a cached source layer joined to a header layer on every solve.

A published unit's facts are assembled (typemap.layers) from two cached parts:

- its source part, keyed on the source's bytes, its authored headers' bytes, the preprocessor command and
  only the interface of its generated headers (their directives and typedefs, which change how the source
  parses and how its types resolve), and on the header layouts its own layouts read;
- one header part per header and version, keyed on the bytes of the header and its include closure.

A land that changes a generated header's declarations or layouts so re-extracts no source: the solve
assembles every unit again from the new header parts, which costs no parse. A unit the layers cannot
represent exactly keeps the whole-unit extraction, keyed on every byte of its include closure.

Large shared values (struct templates, typedef maps) are stored once by digest and interned on load.
"""

from __future__ import annotations

import functools
import hashlib
import itertools
import json
import re
import sys
from collections.abc import Callable, Iterable, Iterator, Mapping
from pathlib import Path
from typing import Any, NamedTuple

from unbake import atomic as atomic_files
from unbake import inputs
from unbake.cache import Cache, key, memo
from unbake.config import Held, Host, Project
from unbake.typemap import declarations, layers, storage

FACTS = "facts"
SHARED = "facts-shared"
SOURCE = "facts-source"
HEADER = "facts-header"
# Bump when the facts extract() produces change for the same inputs. Keys never digest the tool's code.
SCHEMA = 3
_INCLUDE = re.compile(r'^[ \t]*#[ \t]*include[ \t]*([<"])([^>"\n]+)[>"]', re.M)
# Header parts of one version per job: headers in path order share their own include expansions.
HEADERS_PER_JOB = 16
# Shared alias maps and layout templates a process keeps between solves.
SHARED_KEPT = 16384
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
        self._parsed: dict[Path, Parsed] = {}
        self._identifiers: dict[Path, frozenset[str]] = {}
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

    def digest(self, path: Path) -> str:
        found = self._digests.get(path)
        if found is None:
            found = self._digests[path] = inputs.digest(path)
        return found

    def generated(self) -> frozenset[Path]:
        if self._generated is None:
            from unbake.layout import index

            self._generated = frozenset(index.headers(self.project))
        return self._generated

    def parsed(self, path: Path) -> Parsed:
        found = self._parsed.get(path)
        if found is None:
            found = self._parsed[path] = parse(path.read_text())
        return found

    def identifiers(self, path: Path) -> frozenset[str]:
        """Every identifier an authored file spells."""
        found = self._identifiers.get(path)
        if found is None:
            found = self._identifiers[path] = frozenset(_IDENTIFIER.findall(path.read_text(errors="replace")))
        return found


_IDENTIFIER = re.compile(r"[A-Za-z_]\w*")
_KEYWORDS = frozenset(
    [
        "void",
        "int",
        "char",
        "short",
        "long",
        "float",
        "double",
        "unsigned",
        "signed",
        "const",
        "volatile",
        "struct",
        "union",
        "enum",
        "typedef",
    ]
)


class Statement(NamedTuple):
    declared: frozenset[str]
    identifiers: frozenset[str]
    text: str


class Parsed(NamedTuple):
    """A generated header's directives and its typedef and aggregate statements, parsed once."""

    directives: tuple[str, ...]
    statements: tuple[Statement, ...]
    by_name: dict[str, tuple[int, ...]]


def parse(text: str) -> Parsed:
    from unbake.cdecl import declaration_source

    code = declaration_source(text)
    directives = tuple(line.strip() for line in re.findall(r"^[ \t]*#.*$", text, re.M))
    statements: list[Statement] = []
    depth = 0
    start = 0
    for match in re.finditer(r"[{};]", code):
        token = match[0]
        if token == "{":
            depth += 1
        elif token == "}":
            depth -= 1
        elif depth == 0:
            written = " ".join(code[start : match.end()].split())
            start = match.end()
            if re.match(r"(?:typedef\b|(?:struct|union|enum)\b[^;{]*\{)", written):
                statements.append(_statement(written))
    by_name: dict[str, list[int]] = {}
    for index, statement in enumerate(statements):
        for name in statement.declared:
            by_name.setdefault(name, []).append(index)
    return Parsed(directives, tuple(statements), {name: tuple(rows) for name, rows in by_name.items()})


def _statement(text: str) -> Statement:
    """The names a statement declares: its declarators and tags outside any body, and an enum's enumerators."""
    kept, depth = [], 0
    for character in text:
        depth += character == "{"
        if depth == 0 or (character in "{}" and depth == 1):
            kept.append(character)
        depth -= character == "}"
    outside = "".join(kept)
    declared = {
        match[1] for match in re.finditer(r"\b([A-Za-z_]\w*)\s*(?=[;,\[)({])", outside) if match[1] not in _KEYWORDS
    }
    if re.search(r"\benum\b", text):
        declared.update(match[1] for match in re.finditer(r"[{,]\s*([A-Za-z_]\w*)", text))
    return Statement(frozenset(declared), frozenset(_IDENTIFIER.findall(text)), text)


def interface(text: str, names: Iterable[str], *, parsed: Parsed | None = None) -> str:
    """The preprocessor directives of a header, and only those typedef statements and aggregate definitions
    that declare a name in NAMES, one per line, in order. A declared name changes how a text parses only where
    the name appears in that text."""
    parsed = parsed or parse(text)
    wanted = set(names)
    rows = sorted({index for name in wanted & parsed.by_name.keys() for index in parsed.by_name[name]})
    return "\n".join([*parsed.directives, *(parsed.statements[index].text for index in rows)])


def _interfaces(
    snapshot: Snapshot, closure: tuple[Path, ...], own: Path, names: set[str]
) -> list[tuple[Path, str | None]]:
    """For each file of the closure, its interface digest if generated (else None, the caller digests it).

    The names grow to a fixpoint: a selected statement spells names whose own statements it needs, in
    this header or another."""
    generated = snapshot.generated()
    headers = [path for path in closure if path in generated and path != own]
    for path in closure:
        if path not in generated or path == own:
            names |= snapshot.identifiers(path)
    for path in headers:
        for directive in snapshot.parsed(path).directives:
            names.update(_IDENTIFIER.findall(directive))
    taken: dict[Path, set[int]] = {path: set() for path in headers}
    grown = True
    while grown:
        grown = False
        for path in headers:
            parsed = snapshot.parsed(path)
            for name in names & parsed.by_name.keys():
                for index in parsed.by_name[name]:
                    if index not in taken[path]:
                        taken[path].add(index)
                        names |= parsed.statements[index].identifiers
                        grown = True
    return [
        (path, hashlib.sha256(interface("", names, parsed=snapshot.parsed(path)).encode()).hexdigest())
        if path in taken
        else (path, None)
        for path in closure
    ]


def _forced(project: Project, command: list[str]) -> list[Path]:
    forced = [project.root / value for flag, value in itertools.pairwise(command) if flag == "-include"]
    return [path for path in forced if path.is_file()]


def _command(project: Project, policy: Host | None, version: str, *, marked: bool = False) -> list[str]:
    if policy is None:
        return ["in-memory", version, *project.version(version).macros]
    command = declarations._cpp_command(project, policy, version, extra=True, line_markers=marked)
    return [part.replace(str(project.root), ".") for part in command]


def source_key(project: Project, policy: Host | None, task: Task, snapshot: Snapshot) -> str:
    """The whole-unit facts of one task: every byte of the include closure counts."""
    function, source, version = task
    command = _command(project, policy, version)
    roots = (source, *_forced(project, command))
    parts: list[str | bytes] = [FACTS, str(SCHEMA), "source", version, json.dumps(command), function]
    parts.append(storage.relative(project, source))
    parts.append(source.read_bytes())
    for path in snapshot.closure(roots):
        parts.extend((storage.relative(project, path), snapshot.digest(path)))
    return key(*parts)


def unit_key(project: Project, policy: Host | None, source: Path, version: str, snapshot: Snapshot) -> str:
    """A source part: the source's bytes, authored headers' bytes and generated headers' interface only."""
    command = _command(project, policy, version, marked=True)
    roots = (source, *_forced(project, command))
    parts: list[str | bytes] = [SOURCE, str(SCHEMA), "unit", version, json.dumps(command)]
    parts.extend((storage.relative(project, source), source.read_bytes()))
    closure = snapshot.closure(roots)
    names = set(_IDENTIFIER.findall(source.read_text(errors="replace")))
    for path, found in _interfaces(snapshot, closure, source, names):
        state = "interface " + found if found is not None else snapshot.digest(path)
        parts.extend((storage.relative(project, path), state))
    return key(*parts)


def header_key(project: Project, policy: Host | None, header: Path, version: str, snapshot: Snapshot) -> str:
    """A header part: the header's bytes, authored includes' bytes, generated includes' interface only (unit_key's
    scheme, keyed on the header's own identifiers)."""
    command = _command(project, policy, version, marked=True)
    roots = (header, *_forced(project, command))
    parts: list[str | bytes] = [HEADER, str(SCHEMA), version, json.dumps(command), storage.relative(project, header)]
    parts.extend(("self", snapshot.digest(header)))
    closure = snapshot.closure(roots)
    names = set(_IDENTIFIER.findall(header.read_text(errors="replace")))
    for path, found in _interfaces(snapshot, closure, header, names):
        state = "interface " + found if found is not None else snapshot.digest(path)
        parts.extend((storage.relative(project, path), state))
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
        self.documents: dict[tuple[str, str], Any] = {}
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
            value = self.shared[digest] = memo(
                "facts.shared", digest, lambda: self._read_shared(digest), keep=SHARED_KEPT
            )
        return value

    def _read_shared(self, digest: str) -> Any:
        """A shared value by its content digest: read once per process, so a cycle's later solves (one after each
        land) reuse every alias map and layout template an earlier solve read. Seeds only read them."""
        if self.cache is None or (path := self.cache.get(SHARED, digest)) is None:
            raise Held("solve", f"facts.shared: missing {digest}")
        return _load(path)

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

    def raw(self, content_key: str) -> bytes | None:
        """A whole-unit entry's encoded bytes."""
        if self.cache is None:
            rows = self.memory.get(content_key)
            return None if rows is None else json.dumps(rows, separators=(",", ":")).encode()
        path = self.cache.get(FACTS, content_key)
        return None if path is None else path.read_bytes()

    def json_path(self, kind: str, content_key: str) -> Path | None:
        if self.cache is None:
            return Path(content_key) if (kind, content_key) in self.documents else None
        return self.cache.get(kind, content_key)

    def json(self, kind: str, content_key: str) -> Any:
        """A layer part (KIND is SOURCE or HEADER), or None."""
        if self.cache is None:
            return self.documents.get((kind, content_key))
        path = self.cache.get(kind, content_key)
        return None if path is None else _load(path)

    def put_json(self, kind: str, content_key: str, value: Any) -> None:
        if self.cache is None:
            self.documents[(kind, content_key)] = value
        else:
            data = json.dumps(value, separators=(",", ":")).encode()
            self.cache.produce(kind, content_key, functools.partial(_write, data=data))

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


def _include_order(source: Path) -> tuple[tuple[str, ...], str]:
    """Sources whose include lines match sit together, so their header expansions are shared."""
    return tuple(name for _, name in _includes(source, inputs.signature(source))), str(source)


def _headers(project: Project) -> list[Path]:
    return sorted({path.resolve() for root in project.include for path in Path(root).rglob("*.h")})


class _Parts(Mapping[str, dict[str, Any]]):
    """One version's header parts by line-marker path, read from the store on first use."""

    def __init__(self, output: Store, keys: dict[str, str]) -> None:
        self.output, self.content = output, keys
        self.loaded: dict[str, dict[str, Any]] = {}
        self.identities: dict[str, str] = {}

    def __getitem__(self, name: str) -> dict[str, Any]:
        found = self.loaded.get(name)
        if found is None:
            found = self.output.json(HEADER, self.content[name])
            if found is None:
                raise Held("solve", f"facts.headers: missing header part {self.content[name]} for {name}")
            self.loaded[name] = found
        return found

    def identity(self, name: str) -> str:
        """What the part of NAME holds: two versions whose parts hold the same give a unit the same facts. Once per
        part: the stored bytes' digest (a large header's part is megabytes; hashing it per source cost thousands
        of worker CPU-seconds on RW)."""
        found = self.identities.get(name)
        if found is None:
            stored = self.output.json_path(HEADER, self.content[name])
            if stored is not None and stored.is_file():
                found = inputs.digest(stored)
            else:
                found = key(json.dumps(self[name], sort_keys=True))
            self.identities[name] = found
        return found

    def __iter__(self) -> Iterator[str]:
        return iter(self.content)

    def __len__(self) -> int:
        return len(self.content)

    def __contains__(self, name: object) -> bool:
        return name in self.content


def _header_part(project: Project, host: Host | None, version: str, header: Path) -> dict[str, Any]:
    text = declarations.source_unit(project, host, version, header, line_markers=True)
    try:
        return layers.header_part(text, header)
    except Held as error:
        # A header that does not parse alone leaves its includers to whole-unit extraction.
        return {"refused": True, "reason": error.reason}


def _header_job(job: tuple[Project, Host | None, str, list[tuple[str, Path]]]) -> int:
    """Worker body: one version's missing header parts, straight into the shared cache."""
    project, host, version, headers = job
    output = store(host)
    for content_key, header in headers:
        output.put_json(HEADER, content_key, _header_part(project, host, version, header))
    return len(headers)


def _source_tasks(
    project: Project,
    host: Host | None,
    output: Store,
    parts: dict[str, _Parts],
    contexts: dict[str, layers.Context],
    group: list[tuple[int, str, Task]],
    counts: dict[str, int],
    shared: dict[tuple[str, tuple[tuple[str, str], ...]], dict[str, Any] | None],
    snapshot: Snapshot,
) -> list[tuple[int, bytes]] | None:
    """Encoded facts of every task of one source and version: its source part joined to its header parts; None
    when the source needs whole-unit extraction.

    SHARED holds the source's parts by unit text and header-part content: the versions of one source mostly
    preprocess alike, and a part is extracted once for all of them (placeholders keep it version-free)."""
    _, content_key, (_, source, version) = group[0]
    placeholder = _provenance(project, _FUNCTION, _VERSION, source)
    text: str | None = None

    def extracted() -> dict[str, Any] | None:
        nonlocal text
        if text is None:
            text = declarations.source_unit(project, host, version, source, line_markers=True)
        own = layers.path(str(source))
        names = sorted({name for name, _ in layers.Marked(layers.suffix(text), own).runs if name != own})
        identity = (text, tuple((name, parts[version].identity(name)) for name in names if name in parts[version]))
        if identity not in shared:
            counts["sources"] += 1
            shared[identity] = layers.source_part(text, source, parts[version], placeholder)
        if unit_key(project, host, source, version, snapshot) != content_key:
            raise Held("solve", f"facts.inputs: {storage.relative(project, source)} changed during the solve; rerun")
        return shared[identity]

    stub = output.json(SOURCE, content_key)
    part = None
    if stub is None:
        part = extracted()
        stub = {"wide": True} if part is None else {"runs": part["runs"], "named": part["named"]}
        output.put_json(SOURCE, content_key, stub)
    if stub.get("wide"):
        return None
    context = contexts.get(version)
    if context is None:
        context = contexts[version] = layers.Context(parts[version])
    depends = layers.dependencies(context, stub["runs"], source, stub["named"])
    part_key = key(SOURCE, content_key, json.dumps(depends, sort_keys=True))
    if part is None:
        part = output.json(SOURCE, part_key)
    if part is None:
        part = extracted()
        if part is None:
            raise Held("solve", f"facts.layers: {storage.relative(project, source)} became a whole unit; rerun")
    output.put_json(SOURCE, part_key, part)
    source_text = source.read_text()
    result = []
    for index, _, (function, _, _) in group:
        stamped = _provenance(project, function, version, source)

        def provenance(kind: str, row: dict[str, Any] = stamped) -> dict[str, Any]:
            return {**row, "kind": kind}

        consumed, definition = layers.assemble(context, part, source, source_text, provenance)
        result.append((index, output.encoded([consumed, _owned(definition, function)])))
    return result


def _whole_tasks(
    project: Project, host: Host | None, output: Store, group: list[tuple[int, str, Task]], counts: dict[str, int]
) -> list[tuple[int, bytes]]:
    """Whole-unit facts of a source the layers cannot represent, keyed on every byte of its includes. GROUP holds
    the tasks of every version that needs them: source_facts extracts each distinct unit text once."""
    snapshot = Snapshot(project)
    keyed = [(index, source_key(project, host, task, snapshot), task) for index, _, task in group]
    missing = [(content_key, task) for _, content_key, task in keyed if not output.has(content_key)]
    if missing:
        counts["whole"] += 1
        encoded = source_facts(project, host, output, [task for _, task in missing])
        for (content_key, task), data in zip(missing, encoded, strict=True):
            if source_key(project, host, task, Snapshot(project)) != content_key:
                raise Held("solve", f"facts.inputs: {storage.relative(project, task[1])} changed during the solve")
            output.put_encoded(content_key, data)
    result: list[tuple[int, bytes]] = []
    for index, content_key, _ in keyed:
        entry = output.raw(content_key)
        if entry is None:
            raise Held("solve", f"facts.{content_key}: missing after extraction")
        result.append((index, entry))
    return result


# The worker's header parts and layer contexts: the items of one pool job share their header_keys object.
_session: tuple[dict[str, dict[str, str]], dict[str, _Parts], dict[str, layers.Context]] | None = None


def _unit_job(
    job: tuple[Project, Host | None, dict[str, dict[str, str]], list[list[tuple[int, str, Task]]]],
) -> tuple[list[tuple[int, bytes]], dict[str, int]]:
    """Worker body: encoded facts of each task of one source (with all its versions), extracting only missing
    source parts, each distinct one once."""
    global _session
    project, host, header_keys, versions = job
    output = store(host)
    if _session is None or _session[0] is not header_keys:
        _session = (header_keys, {version: _Parts(output, keys) for version, keys in header_keys.items()}, {})
    _, parts, contexts = _session
    counts: dict[str, int] = {"sources": 0, "whole": 0}
    result = []
    shared: dict[tuple[str, tuple[tuple[str, str], ...]], dict[str, Any] | None] = {}
    snapshot = Snapshot(project)
    whole: list[tuple[int, str, Task]] = []
    for group in versions:
        found = _source_tasks(project, host, output, parts, contexts, group, counts, shared, snapshot)
        if found is None:
            whole.extend(group)
        else:
            result.extend(found)
    if whole:
        result.extend(_whole_tasks(project, host, output, whole, counts))
    return result, counts


def published_keys(project: Project, policy: Host | None) -> list[str]:
    """Each task's source-part key: what the solve's facts depend on besides the header layer."""
    snapshot = Snapshot(project)
    return [
        unit_key(project, policy, source, version, snapshot)
        for _, source, version in declarations.published_sources(project)
    ]


def published(project: Project, policy: Host | None, output: Store, keys: list[str]) -> list[dict[str, Any]]:
    """Seeds of every published source in inventory order; keys come from published_keys."""
    tasks = declarations.published_sources(project)
    if len(tasks) != len(keys):
        raise Held("solve", "facts.keys: published sources changed during the solve")
    snapshot = Snapshot(project)
    versions = sorted({version for _, _, version in tasks})
    header_keys: dict[str, dict[str, str]] = {version: {} for version in versions}
    missing_headers: dict[str, list[tuple[str, Path]]] = {version: [] for version in versions}
    if policy is not None:
        for version in versions:
            for header in _headers(project):
                content_key = header_key(project, policy, header, version, snapshot)
                header_keys[version][layers.path(str(header))] = content_key
                if output.json_path(HEADER, content_key) is None:
                    missing_headers[version].append((content_key, header))
    groups: dict[tuple[Path, str], list[tuple[int, str, Task]]] = {}
    for index, (content_key, task) in enumerate(zip(keys, tasks, strict=True)):
        groups.setdefault((task[1], task[2]), []).append((index, content_key, task))
    # Every version of a source goes to one job, so a unit text the versions share is extracted once.
    by_source: dict[Path, list[list[tuple[int, str, Task]]]] = {}
    for group in sorted(groups, key=lambda group: group[1]):
        by_source.setdefault(group[0], []).append(groups[group])
    ordered = [by_source[source] for source in sorted(by_source, key=_include_order)]
    from unbake import pool

    header_jobs = [
        (project, policy, version, headers[start : start + HEADERS_PER_JOB])
        for version, headers in missing_headers.items()
        for start in range(0, len(headers), HEADERS_PER_JOB)
    ]
    if policy is None or output.cache is None:
        results = [_unit_job((project, policy, header_keys, versions)) for versions in ordered]
    else:
        pool.run(policy, _header_job, header_jobs)
        results = pool.run(policy, _unit_job, [(project, policy, header_keys, versions) for versions in ordered])
    encoded: dict[int, bytes] = {}
    counts = {"sources": 0, "whole": 0}
    for found, spent in results:
        encoded.update(found)
        for name, value in spent.items():
            counts[name] += value
    from unbake import effort

    effort.count("facts", counts["sources"], len(groups))
    sys.stderr.write(
        f"facts: {counts['sources']} of {len(groups)} source units extracted, {counts['whole']} whole, "
        f"{sum(map(len, missing_headers.values()))} header parts\n"
    )
    seeds: list[dict[str, Any]] = []
    for index in range(len(tasks)):
        seeds.extend(output.decode(row) for row in json.loads(encoded[index]))
    return seeds
