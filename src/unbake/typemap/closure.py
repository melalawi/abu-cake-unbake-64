"""Value-flow closure: the machine graph built once per map, and the per-solve resolution over it.

Every value-flow edge comes from map facts, so the partition into components is fixed by the map,
the ABI register sets and the registers each callee uses. A Machine holds that partition with its
machine seeds and the record of each component from machine evidence alone; it is cached by exactly
those inputs. A Closure puts the published and declared seeds in front of the machine seeds and
resolves again only the components those seeds, or later seeds, touch.
"""

from __future__ import annotations

import json
import pickle
from collections import defaultdict
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from unbake import pool, tui
from unbake.config import Host
from unbake.typemap import evidence, shards, storage

RANKS = {"machine": 0, "declared": 1, "published": 2, "proven": 3}
# The first page is never mapped: a constant below it is a number or NULL, not the address of an object. Treating
# every `return 0` or NULL argument as the address of one global joined all of them into a single type component.
NULL_PAGE = 0x1000


class Constraints:
    """A union-find over value-flow nodes with their seeds and users."""

    def __init__(self, log: storage.FactLog | None = None) -> None:
        self.log = log
        self.parents: dict[str, str] = {}
        self.sizes: dict[str, int] = {}
        self.seeds: dict[str, dict[str, list[dict[str, Any]]]] = defaultdict(lambda: defaultdict(list))
        self.machine_seeds: dict[str, dict[str, set[tuple[Any, ...]]]] = defaultdict(lambda: defaultdict(set))
        self.users: dict[str, set[str]] = defaultdict(set)
        self.facts: list[dict[str, Any]] = []
        # Call-site edges (caller-side node, callee interface node, evidence), applied by `instantiate`.
        self.links: list[tuple[str, str, dict[str, Any]]] = []
        self.stores: list[tuple[str, str, dict[str, Any]]] = []
        # The keys of each (node, type) seed list: thousands of units seed one global, and a list scan per seed
        # was quadratic in them.
        self._seen: dict[tuple[str, str], set[str]] = defaultdict(set)

    def record(self, fact: dict[str, Any]) -> None:
        if self.log is None:
            self.facts.append(fact)
        else:
            self.log.append(fact)

    def use(self, node: str, user: str | None = None) -> None:
        if node not in self.parents:
            self.parents[node] = node
            self.sizes[node] = 1
        if user:
            self.users[node].add(user)

    def link(self, site: str, interface: str, evidence: dict[str, Any]) -> None:
        """A value flow between a call site and the callee's argument or result, decided once all evidence is known."""
        self.use(site)
        self.use(interface)
        self.links.append((site, interface, evidence))

    def store(self, value: str, cell: str, evidence: dict[str, Any]) -> None:
        """Defer a field store until all uses of that field and its stored values are known."""
        self.use(value)
        self.use(cell)
        self.stores.append((value, cell, evidence))

    def _kinds(self) -> dict[str, set[str]]:
        kinds: dict[str, set[str]] = defaultdict(set)
        for node, types in self.seeds.items():
            kinds[self.root(node)].update(
                type_ for type_, rows in types.items() if any(not row.get("word_transport") for row in rows)
            )
        return kinds

    def instantiate_fields(self) -> None:
        """A shared pointer slot proves its storage type, not equality of all objects stored in it.

        This includes recursive links and reusable command words: merging every stored pointer with the field
        would join otherwise independent users of the aggregate. Keep their value components separate and
        retain the stored type evidence on the field itself. Ordinary scalar and single-source fields still flow.
        """
        cells: dict[str, list[tuple[str, str, dict[str, Any]]]] = defaultdict(list)
        for value, cell, proof in self.stores:
            cells[self.root(cell)].append((value, cell, proof))
        # Settle fields with one source first: a later reusable slot may store a value loaded from one of them.
        for cell, rows in cells.items():
            root = self.root(cell)
            if len({self.root(value) for value, _, _ in rows} - {root}) <= 1:
                for value, target, proof in rows:
                    self.connect(value, target, proof)
        kinds = self._kinds()
        pending = list(cells.values())
        cells.clear()
        for rows in pending:
            for value, cell, proof in rows:
                cells[self.root(cell)].append((value, cell, proof))
        for cell, rows in cells.items():
            field_root = self.root(cell)
            roots = {field_root, *(self.root(value) for value, _, _ in rows)}
            types = set().union(*(kinds.get(root, ()) for root in roots))
            if len(roots - {field_root}) > 1 and any(type_.endswith(" *") for type_ in types):
                for value, target, proof in rows:
                    for type_ in kinds.get(self.root(value), ()):
                        self.seed(target, type_, {"kind": "machine", **proof})
                continue
            for value, target, proof in rows:
                merged = kinds.get(self.root(value), set()) | kinds.get(self.root(target), set())
                self.connect(value, target, proof)
                kinds[self.root(target)] = merged
        self.stores.clear()

    def instantiate(self) -> None:
        """Instantiate generic interfaces together across all aliases of the callee's value.

        Different parameter/result names may describe one value inside a helper. Decide polymorphism for that
        whole component before joining any caller. An untyped or void-pointer helper with independent actuals
        supplies no common pointee contract; each actual retains its own context, even before layouts are named.
        """
        kinds = self._kinds()
        sites: dict[str, list[tuple[str, str, dict[str, Any]]]] = defaultdict(list)
        for site, interface, proof in self.links:
            sites[self.root(interface)].append((site, interface, proof))
        for interface, rows in sites.items():
            callee = self.root(interface)
            roots = {callee, *(self.root(site) for site, _, _ in rows)}
            own = kept_types(kinds.get(callee, ()))
            # Retain each component's kept constraints: an untyped pointer in a caller does not erase a
            # concrete scalar constraint from the callee (or another caller).
            types = set().union(*(kept_types(kinds.get(root, ())) for root in roots))
            if len(roots) > 1 and (len(types) > 1 or (len(roots - {callee}) > 1 and own <= {"void *"})):
                continue
            for site, formal, proof in rows:
                merged = kinds.get(self.root(site), set()) | kinds.get(self.root(formal), set())
                self.connect(site, formal, proof)
                kinds[self.root(formal)] = merged
        self.links.clear()

    def connect(self, left: str, right: str, evidence: dict[str, Any]) -> None:
        self.use(left)
        self.use(right)
        left_root, right_root = self.root(left), self.root(right)
        if left_root != right_root:
            if self.sizes[left_root] < self.sizes[right_root]:
                left_root, right_root = right_root, left_root
            self.parents[right_root] = left_root
            self.sizes[left_root] += self.sizes.pop(right_root)
        provenance = {
            key: evidence[key] for key in ("function", "version", "instruction", "rom_offset") if key in evidence
        }
        self.record({"kind": "value_flow", "left": left, "right": right, "provenance": provenance})

    def seed(self, node: str, type_: str, evidence: dict[str, Any]) -> None:
        self.use(node)
        if evidence.get("kind") == "machine":
            evidence = _machine_evidence(evidence)
            key = tuple(sorted(evidence.items()))
            if key in self.machine_seeds[node][type_]:
                return
            self.machine_seeds[node][type_].add(key)
        else:
            seen = self._seen[(node, type_)]
            identity = _identity(evidence)
            if identity in seen:
                return
            seen.add(identity)
        self.seeds[node][type_].append(evidence)

    def seed_prepared(self, node: str, type_: str, evidence: dict[str, Any], identity: Any) -> None:
        self.use(node)
        seen = self.machine_seeds[node][type_] if evidence.get("kind") == "machine" else self._seen[(node, type_)]
        if identity not in seen:
            seen.add(identity)
            self.seeds[node][type_].append(evidence)

    def root(self, node: str) -> str:
        root = node
        while self.parents[root] != root:
            root = self.parents[root]
        while self.parents[node] != node:
            previous = self.parents[node]
            self.parents[node] = root
            node = previous
        return root

    def groups(self) -> list[list[str]]:
        """Components with sorted members, ordered by their first member."""
        groups: dict[str, list[str]] = {}
        for node in sorted(self.parents):
            groups.setdefault(self.root(node), []).append(node)
        return list(groups.values())


def kept_types(types: Any) -> set[str]:
    """The machine types of one component that survive `resolve`: a word or an untyped pointer proves nothing
    beside a specific type."""
    kept = set(types)
    if any(t.endswith(" *") for t in kept):
        kept -= {"int", "unsigned int"}
        if len(kept) > 1:
            kept.discard("void *")
    return kept


def _identity(evidence: dict[str, Any]) -> str:
    """Equal evidence, equal identity (evidence holds JSON values only)."""
    return json.dumps(evidence, sort_keys=True)


def _machine_evidence(evidence: dict[str, Any]) -> dict[str, Any]:
    keys = ("kind", "function", "version", "instruction", "rom_offset", "common_base", "indexed_base", "word_transport")
    return {key: evidence[key] for key in keys if key in evidence}


def resolve(
    members: list[str], seeds: dict[str, dict[str, list[dict[str, Any]]]], users: dict[str, set[str]]
) -> dict[str, Any]:
    """One component's state from the evidence of its members."""
    found: dict[str, list[dict[str, Any]]] = defaultdict(list)
    seen_users: set[str] = set()
    seed_seen: dict[str, set[Any]] = defaultdict(set)
    for current in members:
        seen_users.update(users.get(current, ()))
        for type_, provenance in seeds.get(current, {}).items():
            for p in provenance:
                key = tuple(sorted(p.items())) if p.get("kind") == "machine" else json.dumps(p, sort_keys=True)
                if key not in seed_seen[type_]:
                    seed_seen[type_].add(key)
                    found[type_].append(p)

    # Proven C overrides machine representations and ordinary declarations.
    # Declared C overrides representations, but inconsistent declarations
    # at the same confidence remain named conflicts.
    def rank(p: dict[str, Any]) -> int:
        return RANKS.get(str(p.get("kind")), 1)

    highest = max((rank(p) for rows in found.values() for p in rows), default=0)
    kept = {t: [p for p in rows if rank(p) == highest] for t, rows in found.items()}
    kept = {t: rows for t, rows in kept.items() if rows}
    if highest == 0 and any(t in kept for t in ("float", "double")):
        # LW/SW move raw O32 words. Unlike integer arithmetic, their signed
        # load opcode does not contradict subsequent floating consumption.
        kept = {
            t: rows
            for t, rows in kept.items()
            if t not in ("int", "unsigned int") or any(not p.get("word_transport") for p in rows)
        }
    if highest == 0 and any(t.endswith(" *") for t in kept):
        # A word load can carry a pointer; narrow/FP uses are incompatible.
        specific = any(t.startswith("struct Shape_") for t in kept)
        kept = {
            t: rows for t, rows in kept.items() if t not in ("int", "unsigned int") and not (specific and t == "void *")
        }
    if len(kept) > 1 and all(
        t in evidence.SCALAR_POINTERS and all(row.get("indexed_base") for row in rows) for t, rows in kept.items()
    ):
        # Accesses of different widths through one pointer prove a pointer, not a pointee.
        kept = {"void *": [p for rows in kept.values() for p in rows]}
    state = "known" if len(kept) == 1 else "conflict" if kept else "unknown"
    return {
        "state": state,
        "type": next(iter(kept)) if len(kept) == 1 else None,
        "provenance": [p for rows in kept.values() for p in rows],
        "users": sorted(seen_users),
        "alternatives": sorted(kept),
    }


@dataclass
class Machine:
    """The value-flow graph of the map: components, machine evidence and the facts derived from it."""

    groups: list[list[str]]
    records: list[dict[str, Any]]
    seeds: dict[str, dict[str, list[dict[str, Any]]]]
    users: dict[str, set[str]]
    neighbours: dict[str, set[str]]
    fields: dict[str, dict[int, list[dict[str, Any]]]]
    shared_fields: dict[str, dict[int, list[dict[str, Any]]]]
    bases: dict[str, str | None]
    arrays: dict[str, dict[str, Any]]
    base_offsets: dict[str, int] = field(default_factory=dict)
    constraints: list[dict[str, Any]] = field(default_factory=list)
    shard: dict[str, Any] | None = None


def origin_node(value: dict[str, Any], addresses: dict[int, list[str]]) -> str | None:
    origins = value.get("origins", [])
    if len(origins) == 1 and origins[0]["offset"] == 0:
        return str(origins[0]["id"])
    constant = value.get("constant")
    if constant is None or constant < NULL_PAGE:
        return None
    names = addresses.get(constant, [])
    return "address:" + names[0] if len(names) == 1 else None


class Recorder:
    """The four writes a body makes to a Constraints, kept as (operation, arguments) to replay in the main process."""

    def __init__(self) -> None:
        self.ops: list[tuple[str, tuple[Any, ...]]] = []

    def use(self, node: str, user: str | None = None) -> None:
        self.ops.append(("use", (node, user)))

    def seed(self, node: str, type_: str, evidence: dict[str, Any]) -> None:
        kept = _machine_evidence(evidence) if evidence.get("kind") == "machine" else evidence
        identity = tuple(sorted(kept.items())) if kept.get("kind") == "machine" else _identity(kept)
        self.ops.append(("seed_prepared", (node, type_, kept, identity)))

    def connect(self, left: str, right: str, evidence: dict[str, Any]) -> None:
        kept = {key: evidence[key] for key in ("function", "version", "instruction", "rom_offset") if key in evidence}
        self.ops.append(("connect", (left, right, kept)))

    def link(self, site: str, interface: str, evidence: dict[str, Any]) -> None:
        kept = {key: evidence[key] for key in ("function", "version", "instruction", "rom_offset") if key in evidence}
        self.ops.append(("link", (site, interface, kept)))

    def store(self, value: str, cell: str, evidence: dict[str, Any]) -> None:
        kept = {key: evidence[key] for key in ("function", "version", "instruction", "rom_offset") if key in evidence}
        self.ops.append(("store", (value, cell, kept)))

    def record(self, fact: dict[str, Any]) -> None:
        self.ops.append(("record", (fact,)))


def _plain(nested: dict[Any, Any]) -> dict[Any, Any]:
    """A defaultdict tree as plain dicts (a lambda default factory cannot cross a process)."""
    return {key: _plain(value) if isinstance(value, dict) else value for key, value in nested.items()}


def _function_rows(
    shared: tuple[Mapping[str, dict[str, Any]], dict[str, dict[str, Any]], dict[str, set[str]], dict[str, Any]],
    names: list[str],
) -> Iterator[tuple[Any, ...]]:
    """The graph writes, fields, memory sources, forwarded values, neighbours and indexed-array candidates of
    each function in NAMES, retaining only a bounded input batch and the current function's output."""
    functions, signatures, used, addresses = shared
    # Keep input expansion bounded independently of the number of functions in a pool piece.
    for function, item in _bodies(functions, names):
        graph = Recorder()
        fields: dict[str, dict[int, list[dict[str, Any]]]] = defaultdict(lambda: defaultdict(list))
        forwarded: dict[str, set[str]] = defaultdict(set)
        memory_sources: dict[str, str] = {}
        neighbour_edges: dict[str, set[str]] = defaultdict(set)
        _function_body(
            graph, function, item, signatures, used, addresses, neighbour_edges, fields, forwarded, memory_sources
        )
        yield (
            function,
            graph.ops,
            _plain(fields),
            memory_sources,
            dict(forwarded),
            neighbour_edges,
            _array_function(function, item, addresses),
        )


# This bounds decoded input, not worker admission or task boundaries. A piece still visits every name once.
BODY_BATCH = 4


def _bodies(functions: Mapping[str, dict[str, Any]], names: list[str]) -> Iterator[tuple[str, dict[str, Any]]]:
    for start in range(0, len(names), BODY_BATCH):
        yield from shards.bodies(functions, names[start : start + BODY_BATCH]).items()


def _function_ops(shared: Any, job: tuple[list[str], Path]) -> Path:
    """Stream one function's operations and side tables at a time; the pool sends back only the private path.

    Separate picklers bound the memo to one function while preserving all sharing inside its result. A retry
    overwrites its own incomplete file; the caller owns the directory until every result has been replayed.
    """
    names, path = job
    with path.open("wb") as output:
        for row in _function_rows(shared, names):
            pickle.dump(row, output, protocol=5)
        pickle.dump(None, output, protocol=5)
    return path


def _read_ops(path: Path) -> Iterator[tuple[Any, ...]]:
    with path.open("rb") as source:
        while (row := pickle.load(source)) is not None:
            yield row
    # EOF before the terminator is an error, including an interrupted write at a function boundary.


@contextmanager
def _operations(shared: Any, pieces: list[list[str]], host: Host | None) -> Iterator[Any]:
    if host is None:
        yield (_function_rows(shared, piece) for piece in pieces)
        return
    root = host.cache_machine_root
    root.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix="unbake-closure-", dir=root) as directory:
        jobs = [(names, Path(directory) / f"{index}.pickle") for index, names in enumerate(pieces)]
        with tui.task("Following values between functions", sum(map(len, pieces))):
            done = pool.run(host, _function_ops, jobs, shared)
        yield (_read_ops(path) for path in done)


def _function_body(
    graph: Any,
    function: str,
    item: dict[str, Any],
    signatures: dict[str, dict[str, Any]],
    used: dict[str, set[str]],
    addresses: dict[str, dict[int, list[str]]],
    neighbours: dict[str, set[str]],
    fields: dict[str, dict[int, list[dict[str, Any]]]],
    forwarded: dict[str, set[str]],
    memory_sources: dict[str, str],
) -> None:
    """The body of build's loop for one function, against a Constraints or a Recorder."""
    for version, body in item["versions"].items():
        for hint in body.get("value_types", []):
            graph.seed(hint["node"], hint["type"], {"kind": "machine", **hint})
        for reg in signatures[function]["registers"]:
            graph.use(f"param:{function}:{reg}", function)
        for call in body["calls"]:
            callee = call["callee"]
            if callee is None:
                continue
            neighbours[function].add(callee)
            neighbours[callee].add(function)
            callee_used = used.get(callee, set())
            for reg, value in call["arguments"].items():
                if reg not in callee_used:
                    continue
                node = origin_node(value, addresses[version])
                formal = f"param:{callee}:{reg}"
                origins = value.get("origins", [])
                storage_source = node
                if storage_source is None and len(origins) == 1 and not origins[0]["id"].startswith("stack:"):
                    storage_source = f"offset:{origins[0]['id']}:{origins[0]['offset']}"
                forwarded[formal].add(storage_source if storage_source is not None else "unknown")
                if node is not None:
                    graph.link(node, formal, call)
                    graph.use(node, function)
                    graph.use(formal, callee)
            index = (call["instruction"] - body["address"]) // 4
            if call.get("tail"):
                for reg in ("r2", "f0"):
                    graph.link(f"result:{function}:{reg}", f"result:{callee}:{reg}", call)
            for reg in ("r2", "r3", "f0", "f2"):
                graph.link(f"return:{function}:{version}:{index}:{reg}", f"result:{callee}:{reg}", call)
        for returned in body["returns"]:
            for reg, value in returned["values"].items():
                node = origin_node(value, addresses[version])
                result_node = f"result:{function}:{reg}"
                graph.use(result_node, function)
                for type_ in value.get("types", []):
                    graph.seed(result_node, type_, {"kind": "machine", **returned})
                if value.get("constant") is not None and reg == "r2":
                    graph.seed(result_node, "int", {"kind": "machine", **returned})
                if node is not None:
                    graph.connect(result_node, node, returned)
                    graph.use(node, function)
        for memory in body["memory"]:
            _access(graph, function, version, body, memory, addresses, fields, memory_sources)


@pool.cpu
def _resolve_job(rows: Any) -> Any:
    return [(index, resolve(members, seeds, users)) for index, members, seeds, users in rows]


def _resolve_groups(groups: Any, seeds: Any, users: Any, indices: Any, host: Host | None) -> Any:
    indices = list(indices)
    if host is None:
        return [(index, resolve(groups[index], seeds, users)) for index in indices]
    pieces = []
    for start in range(0, len(indices), 4096):
        rows = []
        for index in indices[start : start + 4096]:
            members = groups[index]
            rows.append(
                (
                    index,
                    members,
                    {node: seeds[node] for node in members if node in seeds},
                    {node: users[node] for node in members if node in users},
                )
            )
        pieces.append(rows)
    return [row for batch in pool.run(host, _resolve_job, pieces) for row in batch]


def build(
    functions: Mapping[str, dict[str, Any]],
    signatures: dict[str, dict[str, Any]],
    used: dict[str, set[str]],
    addresses: dict[str, dict[int, list[str]]],
    log: storage.FactLog | None,
    host: Host | None = None,
) -> Machine:
    """Every value-flow edge, machine seed and access the map facts imply.

    Each function's body is read and walked in a worker, which records the writes it would make; this process
    replays them into the graph function by function in map order, so the facts are written in the order a serial
    walk writes them."""
    graph = Constraints(log)
    neighbours: dict[str, set[str]] = defaultdict(set)
    fields: dict[str, dict[int, list[dict[str, Any]]]] = defaultdict(lambda: defaultdict(list))
    forwarded: dict[str, set[str]] = defaultdict(set)
    memory_sources: dict[str, str] = {}
    candidates: dict[str, dict[str, Any]] = {}
    names = list(functions)
    shared = (functions, signatures, used, addresses)
    pieces = shards.chunks(names, pool.workers(host) if host is not None else 1)
    with _operations(shared, pieces, host) as done, tui.task("Joining machine value flow"):
        for piece in done:
            for function, ops, local_fields, local_sources, local_forwarded, local_neighbours, arrays in piece:
                for operation, arguments in ops:
                    getattr(graph, operation)(*arguments)
                for origin, offsets in local_fields.items():
                    for offset, accesses in offsets.items():
                        fields[origin][offset].extend(accesses)
                memory_sources.update(local_sources)
                for formal, nodes in local_forwarded.items():
                    forwarded[formal] |= nodes
                for node, others in local_neighbours.items():
                    neighbours[node] |= others
                _merge_arrays(candidates, function, arrays)
        base_cache: dict[str, tuple[str, int] | None] = {}

        def common_base(origin: str, active: frozenset[str] = frozenset()) -> tuple[str, int] | None:
            if origin == "unknown" or origin in active:
                return None
            if origin in base_cache:
                return base_cache[origin]
            if origin.startswith("offset:"):
                source, offset = origin.removeprefix("offset:").rsplit(":", 1)
                root = common_base(source, active | {origin})
                return (root[0], root[1] + int(offset)) if root is not None else None
            if origin in memory_sources:
                root = common_base(memory_sources[origin], active | {origin})
                base_cache[origin] = root
                return root
            if origin.startswith("field:"):
                source, offset = origin.removeprefix("field:").rsplit(":", 1)
                root = common_base(source, active | {origin})
                return (f"field:{root[0]}:{int(offset) + root[1]}", 0) if root is not None else None
            sources = forwarded.get(origin, set())
            if not sources:
                return origin, 0
            roots = {common_base(source, active | {origin}) for source in sources}
            root = next(iter(roots)) if len(roots) == 1 and None not in roots else None
            base_cache[origin] = root
            return root

        bases: dict[str, str | None] = {}
        base_offsets: dict[str, int] = {}
        shared_fields: dict[str, dict[int, list[dict[str, Any]]]] = defaultdict(lambda: defaultdict(list))
        for origin, offsets in fields.items():
            binding = common_base(origin)
            bases[origin] = None if binding is None else binding[0]
            if binding is None:
                continue
            root, bias = binding
            base_offsets[origin] = bias
            for offset, accesses in offsets.items():
                shared_fields[root][offset + bias].extend(accesses)
                if root != origin or bias:
                    graph.connect(f"field:{origin}:{offset}", f"field:{root}:{offset + bias}", accesses[0])
        graph.instantiate_fields()
        graph.instantiate()
        groups = graph.groups()
        seeds = {node: dict(types) for node, types in graph.seeds.items()}
        users = dict(graph.users)
        return Machine(
            groups=groups,
            records=[record for _, record in _resolve_groups(groups, seeds, users, range(len(groups)), host)],
            seeds=seeds,
            users=users,
            neighbours=dict(neighbours),
            fields={origin: dict(offsets) for origin, offsets in fields.items()},
            shared_fields={origin: dict(offsets) for origin, offsets in shared_fields.items()},
            bases=bases,
            base_offsets=base_offsets,
            arrays=candidates,
            constraints=graph.facts,
        )


def _access(
    graph: Constraints,
    function: str,
    version: str,
    body: dict[str, Any],
    memory: dict[str, Any],
    addresses: dict[str, dict[int, list[str]]],
    fields: dict[str, dict[int, list[dict[str, Any]]]],
    memory_sources: dict[str, str],
) -> None:
    index = (memory["instruction"] - body["address"]) // 4
    value_node = f"memory:{function}:{version}:{index}"
    stack_origins = memory["base"].get("origins", [])
    if len(stack_origins) == 1 and stack_origins[0]["id"] == f"stack:{function}":
        slot = stack_origins[0]["offset"] + memory["offset"]
        if slot >= 16 and memory["direction"] == "read":
            scalar = evidence.scalar(memory)
            if scalar:
                formal = evidence.stack_argument(memory, function) or f"stack{slot}"
                loaded = origin_node(memory.get("loaded", {}), addresses[version])
                # A reload after a spill describes its source value, not a
                # fresh incoming parameter occupying the spill's stack slot.
                formal_node = loaded or f"param:{function}:{formal}"
                graph.seed(
                    formal_node, scalar, {"kind": "machine", **memory, "word_transport": memory["opcode"] == 0x23}
                )
                if formal != f"stack{slot}":
                    graph.connect(f"param:{function}:stack{slot}", f"param:{function}:{formal}", memory)
        return
    scalar = evidence.scalar(memory)
    if memory["direction"] == "read" and scalar:
        graph.seed(value_node, scalar, {"kind": "machine", **memory, "word_transport": memory["opcode"] == 0x23})
    if memory.get("indexed") is not None:
        indexed = memory["indexed"]
        names = addresses[version].get(indexed["anchor"], [])
        key = names[0] if len(names) == 1 else f"address:{version}:{indexed['anchor']:08X}"
        cell = "element:" + key
    elif len(memory.get("symbols", [])) == 1:
        cell = "global:" + memory["symbols"][0]
    elif memory.get("address") is not None:
        if memory["address"] < NULL_PAGE:
            return
        cell = f"global:address:{version}:{memory['address']:08X}"
    else:
        origins = memory["base"]["origins"]
        based = memory["base"].get("based", [])
        if not origins and len(based) == 1 and not based[0].startswith("stack:"):
            # A pointer advanced by an index of unknown value: the pointer is proven, its field is not.
            graph.use(based[0], function)
            graph.seed(based[0], evidence.pointer(memory), {"kind": "machine", **memory, "indexed_base": True})
            return
        if len(origins) != 1 or origins[0]["id"].startswith("stack:"):
            return
        origin = origins[0]["id"]
        offset = origins[0]["offset"] + memory["offset"]
        cell = f"field:{origin}:{offset}"
        kept = ("function", "version", "instruction", "rom_offset", "width", "partial", "opcode", "signedness")
        fields[origin][offset].append({key: memory[key] for key in kept})
        graph.use(origin, function)
        graph.seed(origin, "void *", {"kind": "machine", **memory})
    graph.use(cell, function)
    if scalar:
        graph.seed(cell, scalar, {"kind": "machine", **memory, "word_transport": memory["opcode"] == 0x23})
    if memory["direction"] == "read":
        graph.connect(cell, value_node, memory)
        if memory.get("indexed") is None:
            memory_sources[value_node] = cell
    else:
        node = origin_node(memory["value"], addresses[version])
        if node is not None:
            if cell.startswith("field:"):
                graph.store(node, cell, memory)
            else:
                graph.connect(cell, node, memory)
    graph.record(
        {
            "kind": "access",
            "node": cell,
            "width": memory["width"],
            "signedness": memory["signedness"],
            "provenance": {key: memory[key] for key in ("function", "version", "instruction", "rom_offset")},
        }
    )


def _array_function(
    function: str, item: dict[str, Any], addresses: dict[str, dict[int, list[str]]]
) -> dict[str, dict[str, Any]]:
    """One function's indexed anchors: strides, element representations and the accesses, by anchor key."""
    found: dict[str, dict[str, Any]] = {}
    for version, body in item["versions"].items():
        for memory in body["memory"]:
            indexed = memory.get("indexed")
            if indexed is None:
                continue
            names = addresses[version].get(indexed["anchor"], [])
            key = names[0] if len(names) == 1 else f"address:{version}:{indexed['anchor']:08X}"
            candidate = found.setdefault(key, {"elements": set(), "strides": set(), "provenance": []})
            element = evidence.scalar(memory)
            if element:
                candidate["elements"].add(element)
            candidate["strides"].add(indexed["scale"])
            candidate["provenance"].append(
                {"function": function, "version": version, "instruction": memory["instruction"], **indexed}
            )
    return found


def _merge_arrays(candidates: dict[str, dict[str, Any]], function: str, found: dict[str, dict[str, Any]]) -> None:
    """Every indexed anchor's observed strides and element representations, merged in map order."""
    for key, row in found.items():
        candidate = candidates.setdefault(
            key,
            {
                "state": "unknown",
                "type": None,
                "extent": None,
                "strides": [],
                "element_types": [],
                "users": [],
                "provenance": [],
            },
        )
        candidate["element_types"] = sorted(set(candidate["element_types"]) | row["elements"])
        candidate["strides"] = sorted(set(candidate["strides"]) | row["strides"])
        candidate["users"] = sorted(set(candidate["users"]) | {function})
        candidate["provenance"].extend(row["provenance"])


class Closure:
    """Declared seeds in front of a Machine's, resolving again only the components seeds touch."""

    def __init__(self, machine: Machine, declared: Constraints) -> None:
        self.groups = machine.groups
        self.records = machine.records
        self.group = {node: index for index, members in enumerate(self.groups) for node in members}
        self.seeds = machine.seeds
        self.users = machine.users
        self.dirty: set[int] = set()
        self._seen: dict[tuple[str, str], set[str]] = {}
        self.conflicting = {index for index, record in enumerate(self.records) if record["state"] == "conflict"}
        for node in declared.parents:
            self.use(node)
        for node, types in declared.seeds.items():
            merged = {type_: list(rows) for type_, rows in types.items()}
            for type_, rows in self.seeds.get(node, {}).items():
                merged.setdefault(type_, []).extend(rows)
            self.seeds[node] = merged
            self.dirty.add(self.group[node])
        for node, names in declared.users.items():
            if names - self.users.get(node, set()):
                self.users[node] = self.users.get(node, set()) | names
                self.dirty.add(self.group[node])
        self.resolved = {node: self.records[index] for node, index in self.group.items()}

    def use(self, node: str, user: str | None = None) -> None:
        index = self.group.get(node)
        if index is None:
            index = self.group[node] = len(self.groups)
            self.groups.append([node])
            self.records.append({})
            self.dirty.add(index)
        if user and user not in self.users.get(node, ()):
            self.users.setdefault(node, set()).add(user)
            self.dirty.add(index)

    def seed(self, node: str, type_: str, evidence: dict[str, Any]) -> None:
        self.use(node)
        rows = self.seeds.setdefault(node, {}).setdefault(type_, [])
        if evidence.get("kind") == "machine":
            evidence = _machine_evidence(evidence)
        seen = self._seen.get((node, type_))
        if seen is None:
            seen = self._seen[(node, type_)] = {_identity(row) for row in rows}
        identity = _identity(evidence)
        if identity in seen:
            return
        seen.add(identity)
        rows.append(evidence)
        self.dirty.add(self.group[node])

    def close(self, host: Host | None = None) -> list[dict[str, Any]]:
        """Resolve touched components; return every conflict in component order."""
        for index, record in _resolve_groups(self.groups, self.seeds, self.users, self.dirty, host):
            members = self.groups[index]
            self.records[index] = record
            for member in members:
                self.resolved[member] = record
            if record["state"] == "conflict":
                self.conflicting.add(index)
            else:
                self.conflicting.discard(index)
        self.dirty.clear()
        return [
            {"key": "types.conflict:" + self.groups[index][0], "nodes": list(self.groups[index]), **self.records[index]}
            for index in sorted(self.conflicting, key=lambda index: self.groups[index][0])
        ]
