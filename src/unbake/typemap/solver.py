"""Evidence closure with explicit unknowns and named incompatible type constraints."""

from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Iterator
from typing import Any

from unbake.project.config import Held, Policy, Project
from unbake.typemap import abi_declarations, declarations, evidence, layouts, storage
from unbake.typemap.mapping import refresh_map


class Constraints:
    def __init__(self, log: storage.FactLog | None = None) -> None:
        self.log = log
        self.parents: dict[str, str] = {}
        self.sizes: dict[str, int] = {}
        self.seeds: dict[str, dict[str, list[dict[str, Any]]]] = defaultdict(lambda: defaultdict(list))
        self.machine_seeds: dict[str, dict[str, set[tuple[Any, ...]]]] = defaultdict(lambda: defaultdict(set))
        self.users: dict[str, set[str]] = defaultdict(set)
        self.facts: list[dict[str, Any]] = []
        self.resolved: dict[str, dict[str, Any]] = {}

    def record(self, fact: dict[str, Any]) -> None:
        if self.log is None:
            self.facts.append(fact)
        else:
            self.log.append(fact)

    def touch(self, node: str, user: str | None = None) -> None:
        if node not in self.parents:
            self.parents[node] = node
            self.sizes[node] = 1
        if user:
            self.users[node].add(user)

    def connect(self, left: str, right: str, evidence: dict[str, Any]) -> None:
        self.touch(left)
        self.touch(right)
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
        self.touch(node)
        if evidence.get("kind") == "machine":
            evidence = {
                key: evidence[key]
                for key in ("kind", "function", "version", "instruction", "rom_offset", "common_base")
                if key in evidence
            }
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

    def close(self) -> list[dict[str, Any]]:
        groups: dict[str, list[str]] = defaultdict(list)
        for node in sorted(self.parents):
            groups[self.root(node)].append(node)
        conflicts = []
        self.resolved = {}
        for members in groups.values():
            seeds: dict[str, list[dict[str, Any]]] = defaultdict(list)
            users: set[str] = set()
            seed_seen: dict[str, set[Any]] = defaultdict(set)
            for current in members:
                users.update(self.users.get(current, ()))
                for type_, provenance in self.seeds.get(current, {}).items():
                    for p in provenance:
                        key = tuple(sorted(p.items())) if p.get("kind") == "machine" else json.dumps(p, sort_keys=True)
                        if key not in seed_seen[type_]:
                            seed_seen[type_].add(key)
                            seeds[type_].append(p)

            # Proven C overrides machine representations and ordinary declarations.
            # Declared C overrides representations, but inconsistent declarations
            # at the same confidence remain named conflicts.
            def rank(p: dict[str, Any]) -> int:
                return {"machine": 0, "declared": 1, "published": 2, "proven": 3}.get(str(p.get("kind")), 1)

            highest = max((rank(p) for rows in seeds.values() for p in rows), default=0)
            seeds = {t: [p for p in rows if rank(p) == highest] for t, rows in seeds.items()}
            seeds = {t: rows for t, rows in seeds.items() if rows}
            if highest == 0 and any(t.endswith(" *") for t in seeds):
                # A word load can carry a pointer; narrow/FP uses are incompatible.
                specific = any(t.startswith("struct Shape_") for t in seeds)
                seeds = {
                    t: rows
                    for t, rows in seeds.items()
                    if t not in ("int", "unsigned int") and not (specific and t == "void *")
                }
            state = "known" if len(seeds) == 1 else "conflict" if seeds else "unknown"
            record = {
                "state": state,
                "type": next(iter(seeds)) if len(seeds) == 1 else None,
                "provenance": [p for rows in seeds.values() for p in rows],
                "users": sorted(users),
                "alternatives": sorted(seeds),
            }
            for member in members:
                self.resolved[member] = record
            if state == "conflict":
                conflicts.append({"key": "types.conflict:" + min(members), "nodes": sorted(members), **record})
        return conflicts


def origin_node(value: dict[str, Any], addresses: dict[int, list[str]]) -> str | None:
    origins = value.get("origins", [])
    if len(origins) == 1 and origins[0]["offset"] == 0:
        return str(origins[0]["id"])
    constant = value.get("constant")
    if constant is None:
        return None
    names = addresses.get(constant, [])
    return "address:" + names[0] if len(names) == 1 else None


def _merge_records(seeds: list[dict[str, Any]], key: str, graph: Constraints) -> dict[str, Any]:
    records: dict[str, Any] = {}
    index = 0
    while index < len(seeds):
        seed = seeds[index]
        index += 1
        conflicts_before = len(graph.facts)
        for name, record in seed[key].items():
            previous = records.get(name)
            if previous is not None:
                ignored = (
                    "provenance",
                    "prototype",
                    "declaration",
                    "aliases",
                    "typedefs",
                    "registers",
                    "declaration_conflict",
                )
                old = {k: v for k, v in previous.items() if k not in ignored}
                new = {k: v for k, v in record.items() if k not in ignored}
                if key == "functions":
                    # Parameter names have no type meaning.
                    for row in (old, new):
                        row["params"] = [p["type"] for p in row["params"]]
                if old != new:
                    ranks = {"machine": 0, "declared": 1, "published": 2, "proven": 3}
                    old_rank = ranks.get(previous["provenance"].get("kind"), 1)
                    new_rank = ranks.get(record["provenance"].get("kind"), 1)
                    if max(old_rank, new_rank) >= 2 and old_rank != new_rank:
                        graph.facts.append(
                            {
                                "kind": "published_contract_conflict",
                                "entity": f"{key}:{name}",
                                "previous": old,
                                "incoming": new,
                                "provenance": record["provenance"],
                                "resolution": "published contract retained",
                            }
                        )
                        if new_rank > old_rank:
                            records[name] = {**record, "declaration_conflict": False}
                        continue
                if old != new:
                    graph.facts.append(
                        {
                            "kind": "declaration_conflict",
                            "entity": f"{key}:{name}",
                            "previous": old,
                            "incoming": new,
                            "provenance": record["provenance"],
                        }
                    )
                    records[name] = {**previous, "declaration_conflict": True}
                    continue
            records[name] = {**record, "declaration_conflict": bool(previous and previous.get("declaration_conflict"))}
        shared = seed[key]
        if isinstance(shared, declarations.ProvenStructs) and len(graph.facts) == conflicts_before:
            # An uninterrupted run of identical layouts only replaces provenance.
            # Keep the first merge (confidence/conflict handling) and the last
            # receipt. A conflicting run retains every diagnostic as before.
            last = index
            while last < len(seeds):
                following = seeds[last][key]
                if not isinstance(following, declarations.ProvenStructs) or following.template is not shared.template:
                    break
                last += 1
            if last > index:
                index = last - 1
    return records


def _alias_maps(seeds: list[dict[str, Any]], *, shared: bool = False) -> Iterator[dict[str, str]]:
    previous = None
    for seed in seeds:
        current = seed.get("shared_typedefs", seed["aliases"]) if shared else seed["aliases"]
        if current is not previous:
            yield current
        previous = current


def _typedefs(seeds: list[dict[str, Any]], aliases: dict[str, str]) -> dict[str, str]:
    # Retain the last known spelling before resolving it. Canonicalization uses
    # the same final alias environment for every receipt, including overrides.
    known = {
        name: type_
        for values in _alias_maps(seeds, shared=True)
        for name, type_ in values.items()
        if not declarations.unknown(type_)
    }
    return {name: declarations.canonical(type_, aliases) for name, type_ in known.items()}


def infer(
    project: Project,
    facts: dict[str, Any],
    seeds: list[dict[str, Any]],
    *,
    constraint_log: storage.FactLog | None = None,
) -> dict[str, Any]:
    # Sharded mappings decode a complete body on each lookup. Inference revisits
    # callees and callers repeatedly, so load this immutable snapshot once.
    if not isinstance(facts["functions"], dict):
        facts = {**facts, "functions": dict(facts["functions"].items())}
    graph = Constraints(constraint_log)
    aliases = {name: type_ for values in _alias_maps(seeds) for name, type_ in values.items()}
    functions = _merge_records(seeds, "functions", graph)
    globals_ = _merge_records(seeds, "globals", graph)
    structs = _merge_records(seeds, "structs", graph)
    arrays = _merge_records(seeds, "arrays", graph)
    addresses: dict[str, dict[int, list[str]]] = defaultdict(lambda: defaultdict(list))
    for name, record in facts["globals"].items():
        for version, placement in record["versions"].items():
            addresses[version][placement["address"]].append(name)
    for seed in seeds:
        for name, signature in seed["functions"].items():
            for param, reg in zip(signature["params"], signature["registers"], strict=True):
                if reg is not None:
                    graph.touch(f"param:{name}:{reg}", name)
                    if not declarations.unknown(param["type"]):
                        graph.seed(
                            f"param:{name}:{reg}",
                            declarations.canonical(param["type"], aliases),
                            signature["provenance"],
                        )
            type_ = declarations.canonical(signature["return"], aliases)
            register = "f0" if type_ in ("float", "double") else "r2"
            graph.touch(f"result:{name}:{register}", name)
            if type_ != "void" and not declarations.unknown(signature["return"]):
                graph.seed(f"result:{name}:{register}", type_, signature["provenance"])
        for name, record in seed["globals"].items():
            type_ = declarations.canonical(record["type"], aliases)
            graph.touch("global:" + name)
            if not declarations.unknown(record["type"]):
                graph.seed("global:" + name, type_, record["provenance"])
                graph.seed("address:" + name, type_ + " *", record["provenance"])
    declared_returns = {
        name: "f0" if declarations.canonical(record["return"], aliases) in ("float", "double") else "r2"
        for name, record in functions.items()
        if record["return"] != "void"
        and not declarations.unknown(record["return"])
        and not record.get("declaration_conflict")
    }
    signatures = evidence.abi(facts, declared_returns)
    # A previously inferred/declarative signature cannot truncate register use
    # seen in other callers. Published C contracts remain authoritative and the
    # discrepancy is recorded instead of rewriting their prototype.
    for name, signature in list(functions.items()):
        abi = signatures.get(name, {})
        extra = set(abi.get("registers", ())) - set(signature["registers"])
        if extra and signature["arity_known"] and not signature["variadic"]:
            graph.facts.append(
                {
                    "kind": "call_arity_conflict",
                    "entity": "functions:" + name,
                    "registers": sorted(extra),
                    "provenance": signature["provenance"],
                    "resolution": "published contract retained"
                    if signature["provenance"].get("kind") in ("proven", "published")
                    else "use all mapped callers",
                }
            )
            if signature["provenance"].get("kind") not in ("proven", "published"):
                del functions[name]
    neighbours: dict[str, set[str]] = defaultdict(set)
    fields: dict[str, dict[int, list[dict[str, Any]]]] = defaultdict(lambda: defaultdict(list))
    forwarded: dict[str, set[str]] = defaultdict(set)
    memory_sources: dict[str, str] = {}
    for function, item in facts["functions"].items():
        for version, body in item["versions"].items():
            for hint in body.get("value_types", []):
                graph.seed(hint["node"], hint["type"], {"kind": "machine", **hint})
            for reg in signatures[function]["registers"]:
                graph.touch(f"param:{function}:{reg}", function)
            for call in body["calls"]:
                callee = call["callee"]
                if callee is None:
                    continue
                neighbours[function].add(callee)
                neighbours[callee].add(function)
                signature = functions.get(callee)
                used = (
                    set(signature["registers"]) | set(signatures.get(str(callee), {}).get("registers", []))
                    if signature and signature["arity_known"]
                    else set(signatures.get(str(callee), {}).get("registers", []))
                )
                for reg, value in call["arguments"].items():
                    if reg not in used:
                        continue
                    node = origin_node(value, addresses[version])
                    formal = f"param:{callee}:{reg}"
                    forwarded[formal].add(node if node is not None else "unknown")
                    if node is not None:
                        graph.connect(node, formal, call)
                        graph.touch(node, function)
                        graph.touch(formal, callee)
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
                    graph.touch(result_node, function)
                    if value.get("constant") is not None and reg == "r2":
                        graph.seed(result_node, "int", {"kind": "machine", **returned})
                    if node is not None:
                        graph.connect(result_node, node, returned)
                        graph.touch(node, function)
            for memory in body["memory"]:
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
                    continue
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
                        continue
                    origin = origins[0]["id"]
                    offset = origins[0]["offset"] + memory["offset"]
                    cell = f"field:{origin}:{offset}"
                    fields[origin][offset].append(
                        {
                            key: memory[key]
                            for key in (
                                "function",
                                "version",
                                "instruction",
                                "rom_offset",
                                "width",
                                "partial",
                                "opcode",
                                "signedness",
                            )
                        }
                    )
                    graph.touch(origin, function)
                    graph.seed(origin, "void *", {"kind": "machine", **memory})
                graph.touch(cell, function)
                scalar = evidence.scalar(memory)
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
                        "provenance": {
                            key: memory[key] for key in ("function", "version", "instruction", "rom_offset")
                        },
                    }
                )

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

    shared_fields: dict[str, dict[int, list[dict[str, Any]]]] = defaultdict(lambda: defaultdict(list))
    for origin, offsets in fields.items():
        root = common_base(origin)
        if root is None:
            continue
        for offset, accesses in offsets.items():
            shared_fields[root][offset].extend(accesses)
            if root != origin:
                graph.connect(f"field:{origin}:{offset}", f"field:{root}:{offset}", accesses[0])
    graph.close()
    # Authored layouts override machine storage observations.
    layout_index = {name: record for name, record in structs.items()}
    for _name, record in structs.items():
        for alias in record["aliases"]:
            layout_index[alias] = record
    for origin, offsets in shared_fields.items():
        base_type = graph.resolved.get(origin, {}).get("type")
        if not base_type or not base_type.endswith(" *"):
            continue
        base = base_type[:-2].removeprefix("struct ").removeprefix("union ")
        record = layout_index.get(base)
        if record is None or record.get("declaration_conflict"):
            continue
        for offset, accesses in offsets.items():
            members = [f for f in record["fields"] if f["offset"] == offset]
            if (
                len(members) == 1
                and not members[0].get("extent")
                and all(a["width"] == members[0]["size"] and not a["partial"] for a in accesses)
            ):
                graph.seed(
                    f"field:{origin}:{offset}",
                    declarations.canonical(members[0]["type"], aliases),
                    record["provenance"],
                )
    graph.close()
    # Storage types alone cannot select a C prototype with the wrong O32
    # register convention (for example, float bits carried by leading a0).
    for name, abi in signatures.items():
        if name in functions:
            continue
        abi["machine_return_known"] = abi["return_known"]
        abi["machine_arity_known"] = abi["arity_known"]
        types = {reg: graph.resolved.get(f"param:{name}:{reg}", {}).get("type") for reg in abi["registers"]}
        ordered = evidence.parameters(abi["registers"], types)
        if ordered is None:
            abi["arity_known"] = False
        elif all(types[reg] is not None for reg in ordered):
            expected = declarations.parameter_registers([{"type": types[reg]} for reg in ordered], aliases)
            if expected != ordered:
                abi["arity_known"] = False
                abi["conflicts"].append("parameter ABI disagrees with observed scalar representations")
        register = abi["return_register"]
        returned_representation = graph.resolved.get(f"result:{name}:{register}", {}).get("type")
        if (
            returned_representation is not None
            and not abi["void"]
            and (register == "f0") != (returned_representation in ("float", "double"))
        ):
            abi["return_known"] = False
            abi["conflicts"].append("return ABI disagrees with observed scalar representation")
    inferred_structs: dict[str, Any] = {}
    authored_structs = {
        name
        for seed in seeds
        for name in seed.get(
            "authored_structs",
            {
                name
                for name, record in seed["structs"].items()
                if record["provenance"].get("kind") not in ("proven", "published")
            },
        )
    }
    # Partial layouts describe only the observed prefix, never the full object extent.
    # Inferred layouts are named after their first user; ordered origins break ties.
    readable: dict[str, str] = {}
    taken: dict[str, int] = {}
    for origin in sorted(shared_fields):
        users = sorted({access["function"] for accesses in shared_fields[origin].values() for access in accesses})
        if len(users) >= 2:
            base = "Shape_" + users[0]
            taken[base] = taken.get(base, 0) + 1
            readable[origin] = base if taken[base] == 1 else f"{base}_{taken[base]}"
    for origin, offsets in shared_fields.items():
        users = sorted({access["function"] for accesses in offsets.values() for access in accesses})
        base_type = graph.resolved.get(origin, {}).get("type")
        if len(users) < 2:
            continue
        name = readable[origin]

        def unresolved(
            reason: str,
            name: str = name,
            origin: str = origin,
            users: list[str] = users,
            offsets: dict[int, list[dict[str, Any]]] = offsets,
        ) -> None:
            inferred_structs[name] = {
                "state": "unknown",
                "type": None,
                "partial": True,
                "common_base": origin,
                "users": users,
                "size": None,
                "declaration": None,
                "reason": reason,
                "observed_offsets": sorted(offsets),
            }

        # A storage layout is supported by dereferences of this common source,
        # independently of scalar spellings in its value-flow component. Reuse
        # an authored aggregate when present; never overwrite its declaration.
        if base_type and base_type.endswith(" *"):
            base = base_type[:-2].removeprefix("struct ").removeprefix("union ")
            if base in layout_index and (base != name or name in authored_structs):
                continue
        authority = origin
        while authority.startswith("field:"):
            authority = authority.removeprefix("field:").rsplit(":", 1)[0]
        if authority.startswith(("param:", "result:")):
            owner = authority.split(":")[1]
            abi = signatures.get(owner, {})
            if not abi.get("arity_known") or not abi.get("return_known"):
                unresolved("common-base owner ABI is incomplete or conflicting")
                continue
            if any(graph.resolved.get(f"param:{owner}:{reg}", {}).get("state") != "known" for reg in abi["registers"]):
                unresolved("common-base owner parameter types are incomplete or conflicting")
                continue
            if (
                not abi["void"]
                and graph.resolved.get(f"result:{owner}:{abi['return_register']}", {}).get("state") != "known"
            ):
                unresolved("common-base owner return type is incomplete or conflicting")
                continue
        elif authority.startswith("return:"):
            _, caller, version, index, _reg = authority.split(":")
            calls = facts["functions"][caller]["versions"][version]["calls"]
            callee = next(
                (
                    call["callee"]
                    for call in calls
                    if (call["instruction"] - facts["functions"][caller]["versions"][version]["address"]) // 4
                    == int(index)
                ),
                None,
            )
            if not signatures.get(str(callee), {}).get("arity_known") or not signatures.get(str(callee), {}).get(
                "return_known"
            ):
                unresolved("common-base callee ABI is incomplete or conflicting")
                continue
        elif not authority.startswith(("address:", "global:")):
            unresolved("common base is not a global or a known-signature parameter/return")
            continue
        inferred_structs[name] = layouts.observed(name, origin, offsets, graph.resolved, users)
        inferred_structs[name]["base_nodes"] = sorted(
            {origin} | {node for node in fields if common_base(node) == origin}
        )
    shape_components: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for record in inferred_structs.values():
        if record["state"] == "known" and graph.resolved.get(record["common_base"], {}).get("type") == "void *":
            shape_components[id(graph.resolved[record["common_base"]])].append(record)
    for records in shape_components.values():
        # Equality of value types does not establish object identity. Multiple
        # common sources in one type component cannot pick one shared layout.
        if len(records) == 1:
            record = records[0]
            graph.seed(
                record["common_base"], record["type"] + " *", {"kind": "machine", "common_base": record["common_base"]}
            )
    conflicts = graph.close()
    # Closure owns the surviving evidence. Construction indices and discarded
    # lower-confidence seeds are no longer needed during rendering/publication.
    graph.seeds.clear()
    graph.machine_seeds.clear()
    graph.parents.clear()
    graph.sizes.clear()
    conflicts.extend(
        {"key": "types.conflict:" + fact["entity"], **fact}
        for fact in graph.facts
        if fact["kind"] == "declaration_conflict"
    )
    component_names: dict[int, str] = {}
    components: dict[str, dict[str, Any]] = {}
    nodes: dict[str, dict[str, Any]] = {}
    for node, state in sorted(graph.resolved.items()):
        identity = id(state)
        component = component_names.setdefault(identity, node)
        components.setdefault(component, state)
        nodes[node] = {"state": state["state"], "type": state["type"], "component": component}

    def view(node: str, fallback: dict[str, Any]) -> dict[str, Any]:
        state = graph.resolved.get(node)
        if state is None:
            return fallback
        return {
            **nodes[node],
            "provenance": [{"component": nodes[node]["component"]}],
            "users": sorted(graph.users.get(node, ())),
            "alternatives": state["alternatives"],
        }

    unknown = [row for seed in seeds for row in seed.get("unknown", [])]
    output_functions: dict[str, Any] = {}
    for name in sorted(set(facts["functions"]) | set(functions)):
        signature = functions.get(name)
        params = []
        if signature is not None:
            for param, reg in zip(signature["params"], signature["registers"], strict=True):
                node = f"param:{name}:{reg}"
                state = view(
                    node,
                    {"state": "known", "type": param["type"], "provenance": [signature["provenance"]], "users": [name]},
                )
                params.append({"name": param["name"], "register": reg, "node": node, **state})
            register = "f0" if declarations.canonical(signature["return"], aliases) in ("float", "double") else "r2"
            returned = (
                {"state": "known", "type": "void", "provenance": [signature["provenance"]], "users": [name]}
                if signature["return"] == "void"
                else view(f"result:{name}:{register}", {})
            )
            known = (
                signature["arity_known"]
                and not signature.get("declaration_conflict")
                and returned["state"] == "known"
                and all(p["state"] == "known" for p in params)
            )
        else:
            abi = signatures[name]
            observed = abi["registers"]
            types = {reg: graph.resolved.get(f"param:{name}:{reg}", {}).get("type") for reg in observed}
            ordered = evidence.parameters(observed, types)
            for reg in ordered if ordered is not None else observed:
                node = f"param:{name}:{reg}"
                params.append(
                    {
                        "name": f"arg{len(params)}",
                        "register": reg,
                        "node": node,
                        **view(node, {"state": "unknown", "type": None}),
                    }
                )
            returned = (
                {"state": "known", "type": "void", "provenance": [], "users": [name]}
                if abi["void"]
                else view(
                    f"result:{name}:{abi['return_register']}",
                    {"state": "unknown", "type": None, "provenance": [], "users": [name]},
                )
            )
            if returned.get("type") and returned["type"].rstrip().endswith("]"):
                # A declaration of array storage describes the object, not a C
                # scalar return value. Keep the constraint, but do not publish
                # an impossible array-return prototype through value flow.
                returned = {**returned, "state": "unknown", "type": None, "reason": "array storage is not a C return"}
            known = (
                abi["arity_known"]
                and not abi["conflicts"]
                and abi["return_known"]
                and ordered is not None
                and returned["state"] == "known"
                and all(p["state"] == "known" for p in params)
            )
            if returned.get("type") in ("float", "double") and abi["return_register"] != "f0":
                known = False
            for reason in abi["conflicts"]:
                conflicts.append({"key": f"types.conflict:abi:{name}", "reason": reason, "provenance": abi})
        prototype = (
            signature["prototype"]
            if known and signature is not None
            else (
                declarations.declarator(
                    returned["type"],
                    name
                    + "("
                    + (", ".join(declarations.declarator(p["type"], p["name"]) for p in params) or "void")
                    + ")",
                )
                + ";"
                if known
                else None
            )
        )
        output_functions[name] = {
            "state": "known"
            if known
            else "conflict"
            if (signature and signature.get("declaration_conflict"))
            or any(p["state"] == "conflict" for p in params)
            or returned["state"] == "conflict"
            or (not signature and signatures[name]["conflicts"])
            else "unknown",
            "type": prototype,
            "return": returned,
            "params": params,
            "arity": len(params)
            if (signature and signature["arity_known"])
            or (not signature and signatures[name]["arity_known"] and ordered is not None)
            else None,
            "abi": signatures.get(name),
            "versions": list(facts["functions"].get(name, {}).get("versions", {})),
            "provenance": [signature["provenance"]] if signature else [],
            "prototype": prototype,
        }
        if not known:
            output_functions[name]["abi_declaration"] = (
                {
                    "prototype": signature["prototype"],
                    "reasons": ["types.abi.declared: reuse existing C; propagated semantic conflicts remain named"],
                }
                if signature is not None and not signature.get("declaration_conflict")
                else abi_declarations.prototype(name, output_functions[name], aliases)
            )
            carrier = output_functions[name]["abi_declaration"]
            if not carrier.get("prototype") and not signature:
                variants = {}
                abi = signatures[name]
                selected_returns = abi.get("defined_returns", []) if len(abi.get("used_returns", [])) > 1 else []
                for reg in selected_returns:
                    selected = {
                        **output_functions[name],
                        "abi": {
                            **signatures[name],
                            "used_returns": [reg],
                            "return_register": reg,
                            "return_known": True,
                            "machine_return_known": True,
                        },
                        "return": view(f"result:{name}:{reg}", {"state": "unknown", "type": None}),
                    }
                    variant = abi_declarations.prototype(name, selected, aliases)
                    if variant["prototype"]:
                        variant["reasons"].append(
                            f"types.abi.caller_contract: global return conflict; {reg} is proven at every callee exit"
                        )
                        variants[reg] = variant
                if any(not uses for uses in abi.get("caller_return_uses", {}).values()):
                    selected = {
                        **output_functions[name],
                        "abi": {**abi, "used_returns": [], "caller_return_uses": {}},
                        "return": {"state": "unknown", "type": None},
                    }
                    variant = abi_declarations.prototype(name, selected, aliases)
                    if variant["prototype"]:
                        variant["reasons"].append(
                            "types.abi.caller_contract: mapped caller does not consume the unresolved return"
                        )
                        variants["unused"] = variant
                carrier["variants"] = variants
            unknown.append(
                f"function:{name}: signature incomplete or conflicting; arity={output_functions[name]['arity']}"
            )
    output_globals = {
        name: {
            **view("global:" + name, {"state": "unknown", "type": None, "provenance": [], "users": []}),
            "versions": facts["globals"].get(name, {}).get("versions", {}),
            "declaration": globals_.get(name, {}).get("declaration")
            or (
                "extern " + declarations.declarator(graph.resolved["global:" + name]["type"], name) + ";"
                if ":" not in name and graph.resolved.get("global:" + name, {}).get("state") == "known"
                else None
            ),
        }
        for name in sorted(set(facts["globals"]) | set(globals_))
    }
    # A flow component can merge a published cell with a published pointer or
    # array view of its address. That disagreement belongs to the constraints;
    # it cannot remove the cell's own C storage contract from generated headers.
    for name, record in globals_.items():
        if record["provenance"].get("kind") in ("published", "proven"):
            output_globals[name].update(
                state="conflict" if record.get("declaration_conflict") else "known",
                type=declarations.canonical(record["type"], aliases),
                declaration=record["declaration"],
            )
    output_structs = dict(inferred_structs)
    unknown.extend(
        f"struct:{name}: {row.get('reason', 'overlapping or misaligned observed fields')}"
        for name, row in inferred_structs.items()
        if row["state"] != "known"
    )
    type_users: dict[str, set[str]] = defaultdict(set)
    visited_states: set[int] = set()
    for state in graph.resolved.values():
        if state["type"] is not None and id(state) not in visited_states:
            visited_states.add(id(state))
            type_users[state["type"]].update(state["users"])
    for name, record in structs.items():
        # Matched sources include generated headers. Their parsed declarations
        # do not supersede current map-derived bounds and source bindings.
        if name in inferred_structs and name not in authored_structs:
            observed = output_structs[name]
            # Retain proven member names/types as well: storage representations
            # cannot rewrite a declaration used by already matched C.
            members = [field for field in record["fields"] if not field["name"].startswith("padding_")]
            output_structs[name] = {
                **observed,
                "state": "conflict" if record.get("declaration_conflict") else "known",
                "type": record["type"],
                "declaration": record["declaration"],
                "aliases": record.get("aliases", []),
                "typedefs": record.get("typedefs", {}),
                "fields": [
                    {
                        **field,
                        "widths": [field["size"]],
                        "state": "unknown" if field.get("extent") else "known",
                        "type": None if field.get("extent") else field["type"],
                        "reason": "proven array storage" if field.get("extent") else None,
                    }
                    for field in members
                ],
                "minimum_size": max((field["offset"] + field["size"] for field in members), default=0),
                "generated": True,
            }
            continue
        users = sorted(type_users[record["type"]] | type_users[record["type"] + " *"])
        output_structs[name] = {
            **record,
            "state": "conflict" if record.get("declaration_conflict") else "known",
            "generated": name not in authored_structs,
            "users": users,
        }
    for name, record in output_globals.items():
        if record["state"] != "known":
            unknown.append(f"global:{name}: {record['state']}")
    output_arrays = {
        name: {
            **row,
            "state": "conflict"
            if row.get("declaration_conflict")
            else "known"
            if row["extent"] is not None
            else "unknown",
        }
        for name, row in arrays.items()
    }
    for function, item in facts["functions"].items():
        for version, body in item["versions"].items():
            for memory in body["memory"]:
                indexed = memory.get("indexed")
                if indexed is None:
                    continue
                names = addresses[version].get(indexed["anchor"], [])
                key = names[0] if len(names) == 1 else f"address:{version}:{indexed['anchor']:08X}"
                if key in arrays:
                    continue
                candidate = output_arrays.setdefault(
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
    for name, candidate in output_arrays.items():
        if name in arrays:
            continue
        element_state = graph.resolved.get("element:" + name, {})
        candidate["observed_scalar_types"] = candidate["element_types"]
        if element_state.get("state") in ("known", "conflict"):
            candidate["element_types"] = element_state["alternatives"]
        if len(candidate["strides"]) == 1 and len(candidate["element_types"]) == 1:
            type_ = candidate["element_types"][0]
            width = (
                4
                if type_.endswith(" *")
                else {
                    "signed char": 1,
                    "unsigned char": 1,
                    "short": 2,
                    "unsigned short": 2,
                    "int": 4,
                    "unsigned int": 4,
                    "float": 4,
                    "double": 8,
                }.get(type_)
            )
            if candidate["strides"][0] == width:
                candidate.update(state="known", type=type_, partial=True)
                if name in output_globals and name not in globals_:
                    output_globals[name].update(state="known", type=type_ + " []", declaration=None)
        if len(candidate["strides"]) > 1 or len(candidate["element_types"]) > 1:
            candidate["state"] = "conflict"
            conflicts.append(
                {
                    "key": f"types.conflict:array:{name}",
                    "strides": candidate["strides"],
                    "alternatives": candidate["element_types"],
                }
            )
    unknown.extend(
        f"array:{name}: element/extent unresolved" for name, row in output_arrays.items() if row["state"] != "known"
    )
    dependencies = {name: sorted(neighbours[name]) for name in facts["functions"]}
    return {
        "typedefs": _typedefs(seeds, aliases),
        "functions": output_functions,
        "globals": output_globals,
        "structs": output_structs,
        "arrays": output_arrays,
        "constraints": graph.facts,
        "nodes": nodes,
        "components": components,
        "conflicts": conflicts,
        "unknown": sorted(set(unknown)),
        "dependencies": dependencies,
    }


def solve(project: Project, policy: Policy | None = None, *, facts: dict[str, Any] | None = None) -> dict[str, Any]:
    from unbake.typemap.abi_facts import refine

    database = project.build / "types/database.json"
    summary = project.build / "types/summary.json"
    previous = {}
    if database.is_file():
        if summary.is_file():
            previous = storage.read(summary, "types.summary")
            recovered = storage.validate_identity(project, previous, "types.summary")
            if previous.get("database_sha256") != storage.file_digest(database):
                if not recovered:
                    raise Held("solve", "types.summary: database changed independently of its semantic index")
                # Foreign checkout evidence can have a stale semantic index.
                # Rebuild from pinned map/source facts; reuse neither old file.
                previous = {}
        elif database.stat().st_size <= 64 * 1024 * 1024:
            previous = storage.read(database, "types.database")
            storage.validate_identity(project, previous, "types.database")
        else:
            raise Held("solve", "types.summary: missing bounded semantic index for existing database")
    facts = refine(project, refresh_map(project) if facts is None else facts)
    pinned = storage.inputs(project, headers=True)
    log = storage.FactLog(project.build / "types")
    try:
        result = infer(project, facts, declarations.collect(project, policy), constraint_log=log)
        from unbake.typemap import declaration_evidence

        result["declaration_evidence"] = {
            str(path.relative_to(project.include[0])): text
            for path, text in declaration_evidence.feedback_components(project).items()
        }
        published, homes = declaration_evidence.published_snapshot(project)
        result["published_declarations"] = {
            str(path.relative_to(project.include[0])): text for path, text in published.items()
        }
        result["published_homes"] = {
            str(path.relative_to(project.include[0])): sorted(
                str(home.relative_to(project.include[0])) for home in paths
            )
            for path, paths in homes.items()
        }
        result["constraints"] = [log.finish(project.root), *result["constraints"]]
    finally:
        log.close()
    revision = int(previous.get("revision", 0)) + 1
    result = {
        **storage.identity(project),
        "map_sha256": storage.file_digest(project.build / "map/facts.json"),
        "map_shard": facts["shard"],
        "map_shard_sha256": facts["shard_sha256"],
        "abi_supplement": facts.get("abi_supplement"),
        "inputs_sha256": pinned,
        "revision": revision,
        **result,
    }
    if pinned != storage.inputs(project, headers=True):
        raise Held("solve", "types.inputs_stale: declarations changed during solve")
    from unbake.typemap.database import publish

    publish(project, result, previous, policy=policy)
    return result
