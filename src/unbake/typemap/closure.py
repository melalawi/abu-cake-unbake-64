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
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from unbake import atomic as atomic_files
from unbake.cache import Cache
from unbake.config import Held
from unbake.typemap import evidence, storage

RANKS = {"machine": 0, "declared": 1, "published": 2, "proven": 3}


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
        elif evidence in self.seeds[node][type_]:
            return
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


def _machine_evidence(evidence: dict[str, Any]) -> dict[str, Any]:
    keys = ("kind", "function", "version", "instruction", "rom_offset", "common_base")
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
    if highest == 0 and any(t.endswith(" *") for t in kept):
        # A word load can carry a pointer; narrow/FP uses are incompatible.
        specific = any(t.startswith("struct Shape_") for t in kept)
        kept = {
            t: rows for t, rows in kept.items() if t not in ("int", "unsigned int") and not (specific and t == "void *")
        }
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
    constraints: list[dict[str, Any]] = field(default_factory=list)
    shard: dict[str, Any] | None = None


def origin_node(value: dict[str, Any], addresses: dict[int, list[str]]) -> str | None:
    origins = value.get("origins", [])
    if len(origins) == 1 and origins[0]["offset"] == 0:
        return str(origins[0]["id"])
    constant = value.get("constant")
    if constant is None:
        return None
    names = addresses.get(constant, [])
    return "address:" + names[0] if len(names) == 1 else None


def build(
    facts: dict[str, Any],
    signatures: dict[str, dict[str, Any]],
    used: dict[str, set[str]],
    addresses: dict[str, dict[int, list[str]]],
    log: storage.FactLog | None,
) -> Machine:
    """Every value-flow edge, machine seed and access the map facts imply."""
    graph = Constraints(log)
    neighbours: dict[str, set[str]] = defaultdict(set)
    fields: dict[str, dict[int, list[dict[str, Any]]]] = defaultdict(lambda: defaultdict(list))
    forwarded: dict[str, set[str]] = defaultdict(set)
    memory_sources: dict[str, str] = {}
    for function, item in facts["functions"].items():
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
                    forwarded[formal].add(node if node is not None else "unknown")
                    if node is not None:
                        graph.connect(node, formal, call)
                        graph.use(node, function)
                        graph.use(formal, callee)
                index = (call["instruction"] - body["address"]) // 4
                if call.get("tail"):
                    for reg in ("r2", "f0"):
                        graph.connect(f"result:{function}:{reg}", f"result:{callee}:{reg}", call)
                for reg in ("r2", "r3", "f0", "f2"):
                    graph.connect(f"result:{callee}:{reg}", f"return:{function}:{version}:{index}:{reg}", call)
            for returned in body["returns"]:
                for reg, value in returned["values"].items():
                    node = origin_node(value, addresses[version])
                    result_node = f"result:{function}:{reg}"
                    graph.use(result_node, function)
                    if value.get("constant") is not None and reg == "r2":
                        graph.seed(result_node, "int", {"kind": "machine", **returned})
                    if node is not None:
                        graph.connect(result_node, node, returned)
                        graph.use(node, function)
            for memory in body["memory"]:
                _access(graph, function, version, body, memory, addresses, fields, memory_sources)

    base_cache: dict[str, str | None] = {}

    def common_base(origin: str, active: frozenset[str] = frozenset()) -> str | None:
        if origin == "unknown" or origin in active:
            return None
        if origin in base_cache:
            return base_cache[origin]
        if origin in memory_sources:
            root = common_base(memory_sources[origin], active | {origin})
            base_cache[origin] = root
            return root
        if origin.startswith("field:"):
            source, offset = origin.removeprefix("field:").rsplit(":", 1)
            root = common_base(source, active | {origin})
            return f"field:{root}:{offset}" if root is not None else None
        sources = forwarded.get(origin, set())
        if not sources:
            return origin
        roots = {common_base(source, active | {origin}) for source in sources}
        root = next(iter(roots)) if len(roots) == 1 and None not in roots else None
        base_cache[origin] = root
        return root

    bases: dict[str, str | None] = {}
    shared_fields: dict[str, dict[int, list[dict[str, Any]]]] = defaultdict(lambda: defaultdict(list))
    for origin, offsets in fields.items():
        root = bases[origin] = common_base(origin)
        if root is None:
            continue
        for offset, accesses in offsets.items():
            shared_fields[root][offset].extend(accesses)
            if root != origin:
                graph.connect(f"field:{origin}:{offset}", f"field:{root}:{offset}", accesses[0])
    groups = graph.groups()
    seeds = {node: dict(types) for node, types in graph.seeds.items()}
    users = dict(graph.users)
    return Machine(
        groups=groups,
        records=[resolve(members, seeds, users) for members in groups],
        seeds=seeds,
        users=users,
        neighbours=dict(neighbours),
        fields={origin: dict(offsets) for origin, offsets in fields.items()},
        shared_fields={origin: dict(offsets) for origin, offsets in shared_fields.items()},
        bases=bases,
        arrays=_array_candidates(facts, addresses),
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
                graph.seed(f"param:{function}:{formal}", scalar, {"kind": "machine", **memory})
                if formal != f"stack{slot}":
                    graph.connect(f"param:{function}:stack{slot}", f"param:{function}:{formal}", memory)
        return
    scalar = evidence.scalar(memory)
    if memory["direction"] == "read" and scalar:
        graph.seed(value_node, scalar, {"kind": "machine", **memory})
    if memory.get("indexed") is not None:
        indexed = memory["indexed"]
        names = addresses[version].get(indexed["anchor"], [])
        key = names[0] if len(names) == 1 else f"address:{version}:{indexed['anchor']:08X}"
        cell = "element:" + key
    elif len(memory.get("symbols", [])) == 1:
        cell = "global:" + memory["symbols"][0]
    elif memory.get("address") is not None:
        cell = f"global:address:{version}:{memory['address']:08X}"
    else:
        origins = memory["base"]["origins"]
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
        graph.seed(cell, scalar, {"kind": "machine", **memory})
    if memory["direction"] == "read":
        graph.connect(cell, value_node, memory)
        if memory.get("indexed") is None:
            memory_sources[value_node] = cell
    else:
        node = origin_node(memory["value"], addresses[version])
        if node is not None:
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


def _array_candidates(facts: dict[str, Any], addresses: dict[str, dict[int, list[str]]]) -> dict[str, dict[str, Any]]:
    """Every indexed anchor's observed strides and element representations, in map order."""
    candidates: dict[str, dict[str, Any]] = {}
    for function, item in facts["functions"].items():
        for version, body in item["versions"].items():
            for memory in body["memory"]:
                indexed = memory.get("indexed")
                if indexed is None:
                    continue
                names = addresses[version].get(indexed["anchor"], [])
                key = names[0] if len(names) == 1 else f"address:{version}:{indexed['anchor']:08X}"
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
                element = evidence.scalar(memory)
                if element:
                    candidate["element_types"] = sorted(set(candidate["element_types"]) | {element})
                candidate["strides"] = sorted(set(candidate["strides"]) | {indexed["scale"]})
                candidate["users"] = sorted(set(candidate["users"]) | {function})
                candidate["provenance"].append(
                    {"function": function, "version": version, "instruction": memory["instruction"], **indexed}
                )
    return candidates


class Closure:
    """Declared seeds in front of a Machine's, resolving again only the components seeds touch."""

    def __init__(self, machine: Machine, declared: Constraints) -> None:
        self.groups = machine.groups
        self.records = machine.records
        self.group = {node: index for index, members in enumerate(self.groups) for node in members}
        self.seeds = machine.seeds
        self.users = machine.users
        self.dirty: set[int] = set()
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
            key = tuple(sorted(evidence.items()))
            if any(row.get("kind") == "machine" and tuple(sorted(row.items())) == key for row in rows):
                return
        elif evidence in rows:
            return
        rows.append(evidence)
        self.dirty.add(self.group[node])

    def close(self) -> list[dict[str, Any]]:
        """Resolve touched components; return every conflict in component order."""
        for index in self.dirty:
            members = self.groups[index]
            record = self.records[index] = resolve(members, self.seeds, self.users)
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


def cached(cache: Cache | None, kind: str, parts: list[str], compute: Callable[[], Any]) -> Any:
    """compute(), or the value an earlier solve stored for the same inputs."""
    if cache is None:
        return compute()
    from unbake.cache import key

    content_key = key(kind, *parts)
    path = cache.get(kind, content_key)
    if path is not None:
        return load(path)
    value = compute()
    cache.produce(kind, content_key, lambda target: atomic_files.fresh(target, pickle.dumps(value, protocol=5)))
    return value


def load(path: Path) -> Any:
    try:
        return pickle.loads(path.read_bytes())
    except (OSError, pickle.UnpicklingError, EOFError, AttributeError, ValueError) as error:
        raise Held("solve", f"types.cache: unreadable entry {path}: {error}; delete it and rerun") from error
