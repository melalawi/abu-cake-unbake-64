"""Evidence closure with explicit unknowns and named incompatible type constraints."""

from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

from unbake import atomic as atomic_files
from unbake import cache as content_cache
from unbake import inputs
from unbake.cache import Cache
from unbake.config import Host, Project
from unbake.typemap import abi_declarations, closure, declarations, evidence, layouts, shards, storage
from unbake.typemap.closure import Constraints
from unbake.typemap.mapping import refresh_map

# Bump when this step's output changes for the same inputs. Keys never digest the tool's code.
SCHEMA = 3


_MERGE_IGNORED = ("provenance", "prototype", "declaration", "aliases", "typedefs", "registers", "declaration_conflict")


_RANKS = {"machine": 0, "declared": 1, "published": 2, "proven": 3}


def _rank(record: dict[str, Any]) -> int:
    return _RANKS.get(record["provenance"].get("kind"), 1)


def _comparable(
    record: dict[str, Any], key: str, aliases: dict[str, str], canonical: Callable[[str], str] | None = None
) -> dict[str, Any]:
    """A record's type meaning: typedef names resolved through its own seed's aliases (s32 and int are one type),
    parameter names dropped. CANONICAL is canonical() bound to ALIASES (a merge passes a remembering one)."""
    resolve = canonical or (lambda type_: declarations.canonical(type_, aliases))
    row = {k: v for k, v in record.items() if k not in _MERGE_IGNORED}
    if key == "functions":
        row["params"] = [resolve(p["type"]) for p in row["params"]]
    for field in ("type", "return"):
        if isinstance(row.get(field), str):
            row[field] = resolve(row[field])
    return row


def _stripped(record: dict[str, Any], key: str) -> dict[str, Any]:
    """The fields a conflict names: everything but provenance and spelling, parameter types without names."""
    row = {k: v for k, v in record.items() if k not in _MERGE_IGNORED}
    if key == "functions":
        row["params"] = [p["type"] for p in row["params"]]
    return row


class _Canonical:
    """canonical() per alias environment, remembered for one merge: seeds share interned alias maps, so one
    environment object serves thousands of seeds. The map is held, so its id stays its own."""

    def __init__(self) -> None:
        self._known: dict[int, tuple[dict[str, str], dict[str, str]]] = {}

    def bound(self, aliases: dict[str, str]) -> Callable[[str], str]:
        entry = self._known.get(id(aliases))
        if entry is None or entry[0] is not aliases:
            entry = self._known[id(aliases)] = (aliases, {})
        known = entry[1]

        def resolve(type_: str) -> str:
            found = known.get(type_)
            if found is None:
                found = known[type_] = declarations.canonical(type_, aliases)
            return found

        return resolve


def _merge_records(seeds: list[dict[str, Any]], key: str, graph: Constraints) -> dict[str, Any]:
    records: dict[str, Any] = {}
    meanings: dict[str, dict[str, Any]] = {}
    # Per name, a spelling of its current meaning and the alias map it was read in: thousands of units consume one
    # header contract spelled alike in one shared alias map, and the same spelling in the same map has the same
    # meaning, so only a new spelling or map pays for canonical().
    spellings: dict[str, tuple[dict[str, Any], dict[str, str]]] = {}
    environments = _Canonical()
    index = 0
    while index < len(seeds):
        seed = seeds[index]
        index += 1
        conflicts_before = len(graph.facts)
        aliases = seed.get("aliases", {})
        canonical = environments.bound(aliases)
        for name, record in seed[key].items():
            previous = records.get(name)
            if previous is not None:
                spelled = spellings[name]
                stripped = _stripped(record, key)
                if (spelled[1] is aliases and spelled[0] == stripped) or (
                    meanings[name] == _comparable(record, key, aliases, canonical)
                ):
                    spellings[name] = stripped, aliases
                    # The same type, maybe spelled through a typedef: no conflict. As for identical records,
                    # the incoming one is kept unless it ranks lower, so a published contract replaces a
                    # declared one (and the published-storage rule then keeps it in the header).
                    if _rank(record) >= _rank(previous):
                        records[name] = {**record, "declaration_conflict": bool(previous.get("declaration_conflict"))}
                    continue
                old, new = _stripped(previous, key), stripped
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
                            meanings[name] = _comparable(record, key, aliases, canonical)
                            spellings[name] = stripped, aliases
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
            meanings[name] = _comparable(record, key, aliases, canonical)
            spellings[name] = _stripped(record, key), aliases
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
    cache: Cache | None = None,
    shard_dir: Path | None = None,
) -> dict[str, Any]:
    """Resolve every value-flow component from declared seeds over the map's machine graph.

    With shard_dir, machine constraints stream to a constraints shard there; with cache, the ABI and
    the machine graph are reused from an earlier solve with the same map, ABI and register inputs.
    """
    mapped = facts["functions"]
    inventory = getattr(mapped, "inventory", mapped)
    decoded: dict[str, Any] = {}

    def bodies() -> dict[str, Any]:
        # Sharded mappings decode a complete body on each lookup; decode once, only on a cache miss.
        if not decoded:
            decoded.update(mapped.read(list(mapped)) if isinstance(mapped, shards.Functions) else mapped.items())
        return {**facts, "functions": decoded}

    def versions(name: str) -> list[str]:
        if isinstance(mapped, shards.Functions):
            return sorted(inventory.get(name, {}).get("versions", {}))
        return list((decoded if decoded else mapped).get(name, {}).get("versions", {}))

    declared = Constraints()
    aliases = {name: type_ for values in _alias_maps(seeds) for name, type_ in values.items()}
    functions = _merge_records(seeds, "functions", declared)
    globals_ = _merge_records(seeds, "globals", declared)
    structs = _merge_records(seeds, "structs", declared)
    arrays = _merge_records(seeds, "arrays", declared)
    addresses: dict[str, dict[int, list[str]]] = defaultdict(lambda: defaultdict(list))
    for name, record in facts["globals"].items():
        for version, placement in record["versions"].items():
            addresses[version][placement["address"]].append(name)
    # canonical() under the one solve-wide alias map, once per spelling: thousands of seeds repeat each type.
    resolve = _Canonical().bound(aliases)
    for seed in seeds:
        for name, signature in seed["functions"].items():
            for param, reg in zip(signature["params"], signature["registers"], strict=True):
                if reg is not None:
                    declared.use(f"param:{name}:{reg}", name)
                    if not declarations.unknown(param["type"]):
                        declared.seed(
                            f"param:{name}:{reg}",
                            resolve(param["type"]),
                            signature["provenance"],
                        )
            type_ = resolve(signature["return"])
            register = "f0" if type_ in ("float", "double") else "r2"
            declared.use(f"result:{name}:{register}", name)
            if type_ != "void" and not declarations.unknown(signature["return"]):
                declared.seed(f"result:{name}:{register}", type_, signature["provenance"])
        for name, record in seed["globals"].items():
            type_ = resolve(record["type"])
            declared.use("global:" + name)
            if not declarations.unknown(record["type"]):
                declared.seed("global:" + name, type_, record["provenance"])
                declared.seed("address:" + name, type_ + " *", record["provenance"])
    declared_returns = {
        name: "f0" if resolve(record["return"]) in ("float", "double") else "r2"
        for name, record in functions.items()
        if record["return"] != "void"
        and not declarations.unknown(record["return"])
        and not record.get("declaration_conflict")
    }
    map_parts = _map_parts(facts, inventory) if cache is not None else []
    signatures = closure.cached(
        cache,
        "types-abi",
        [str(SCHEMA), *map_parts, json.dumps(sorted(declared_returns.items()))],
        lambda: evidence.abi(bodies(), declared_returns),
    )
    # A previously inferred/declarative signature cannot truncate register use
    # seen in other callers. Published C contracts remain authoritative and the
    # discrepancy is recorded instead of rewriting their prototype.
    for name, signature in list(functions.items()):
        abi = signatures.get(name, {})
        extra = set(abi.get("registers", ())) - set(signature["registers"])
        if extra and signature["arity_known"] and not signature["variadic"]:
            declared.facts.append(
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
    used: dict[str, set[str]] = {}
    for name in set(functions) | set(signatures):
        signature = functions.get(name)
        observed = set(signatures.get(name, {}).get("registers", []))
        used[name] = set(signature["registers"]) | observed if signature and signature["arity_known"] else observed
    machine, shard = _machine(
        project,
        cache,
        shard_dir,
        [
            str(SCHEMA),
            *map_parts,
            json.dumps(sorted((name, row["registers"]) for name, row in signatures.items())),
            json.dumps(sorted((name, sorted(registers)) for name, registers in used.items())),
        ],
        lambda log: closure.build(bodies(), signatures, used, addresses, log),
    )
    graph = closure.Closure(machine, declared)
    neighbours = machine.neighbours
    fields = machine.fields
    shared_fields = machine.shared_fields
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
                    resolve(members[0]["type"]),
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
            body = (decoded[caller] if caller in decoded else mapped[caller])["versions"][version]
            callee = next(
                (
                    call["callee"]
                    for call in body["calls"]
                    if (call["instruction"] - body["address"]) // 4 == int(index)
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
            {origin} | {node for node in fields if machine.bases[node] == origin}
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
    # Closure owns the surviving evidence. Discarded lower-confidence seeds
    # are no longer needed during rendering/publication.
    graph.seeds.clear()
    conflicts.extend(
        {"key": "types.conflict:" + fact["entity"], **fact}
        for fact in declared.facts
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
    for name in sorted(set(inventory) | set(functions)):
        signature = functions.get(name)
        params = []
        returned: dict[str, Any]
        if signature is not None:
            for param, reg in zip(signature["params"], signature["registers"], strict=True):
                node = f"param:{name}:{reg}"
                state = view(
                    node,
                    {"state": "known", "type": param["type"], "provenance": [signature["provenance"]], "users": [name]},
                )
                params.append({"name": param["name"], "register": reg, "node": node, **state})
            register = "f0" if resolve(signature["return"]) in ("float", "double") else "r2"
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
            "versions": versions(name),
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
                type=resolve(record["type"]),
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
    for key, candidate in machine.arrays.items():
        if key not in arrays:
            output_arrays[key] = candidate
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
    dependencies = {name: sorted(neighbours.get(name, ())) for name in inventory}
    return {
        "typedefs": _typedefs(seeds, aliases),
        "functions": output_functions,
        "globals": output_globals,
        "structs": output_structs,
        "arrays": output_arrays,
        "constraints": [*shard, *declared.facts, *machine.constraints],
        "nodes": nodes,
        "components": components,
        "conflicts": conflicts,
        "unknown": sorted(set(unknown)),
        "dependencies": dependencies,
    }


def _map_parts(facts: dict[str, Any], inventory: Any) -> list[str]:
    """What the ABI and machine graph read from the map: the shard, its supplement, inventory and globals."""
    return [
        facts["shard_sha256"],
        json.dumps(facts.get("abi_supplement"), sort_keys=True),
        storage.digest(storage.encoded(dict(inventory))),
        storage.digest(storage.encoded(facts["globals"])),
    ]


def _machine(
    project: Project,
    cache: Cache | None,
    shard_dir: Path | None,
    parts: list[str],
    make: Any,
) -> tuple[closure.Machine, list[dict[str, Any]]]:
    """The machine graph and its constraints shard row (none without shard_dir)."""

    def build() -> closure.Machine:
        if shard_dir is None:
            built: closure.Machine = make(None)
            return built
        log = storage.FactLog(shard_dir)
        try:
            built = make(log)
            built.shard = log.finish(project.root)
        finally:
            log.close()
        if cache is not None:
            cache.put("types-constraints", built.shard["sha256"], project.root / built.shard["path"])
        return built

    machine: closure.Machine = closure.cached(cache, "types-machine", parts, build)
    if shard_dir is None:
        return machine, []
    if machine.shard is None or not _installed(project, cache, machine.shard):
        machine = build()
    assert machine.shard is not None
    return machine, [machine.shard]


def _installed(project: Project, cache: Cache | None, row: dict[str, Any]) -> bool:
    """The constraints shard is in place, restored from the cache if it went missing."""
    path = project.root / row["path"]
    if path.is_file() and inputs.digest(path) == row["sha256"]:
        return True
    entry = None if cache is None else cache.get("types-constraints", row["sha256"])
    if entry is None or inputs.digest(entry) != row["sha256"]:
        return False
    atomic_files.copyfile(entry, path, durable=False)
    return True


def _types_key(project: Project, policy: Host | None, facts: dict[str, Any], fact_keys: list[str]) -> str:
    """Every input of merge, infer and header publication, the generated headers included.

    The solve reads the generated headers (their header facts, published declarations and evidence), which it
    and the headers step write. Keyed on them, a solve whose publish changed them runs once more on what it
    wrote: the step settles only at a fixed point, so a warm tree solves as a cold solve of it would, and a
    solve that never settles is refused by name (steps: input key changes on every run)."""
    files = [project.root / "config.toml", project.root / "layout.toml"]
    files += [path for root in project.include for path in sorted(root.rglob("*")) if path.is_file()]
    # Of versions/, the solve reads each version's split and symbols only: slices.mk and report.json are
    # buildfiles' and progress' outputs, and keying on them reran types after both.
    files += [
        path for name in project.versions for path in (project.version(name).split, project.version(name).symbols)
    ]
    parts: list[str] = ["types", str(SCHEMA), facts["shard_sha256"], json.dumps(facts.get("abi_supplement"))]
    if policy is not None:
        parts.append(json.dumps([str(policy.cpp), *project.cppflags]))
    parts.extend(fact_keys)
    for path in files:
        parts.append(storage.relative(project, path))
        parts.append(inputs.digest(path) if path.is_file() else "missing")
    return content_cache.key(*parts)


def input_key(project: Project, host: Host | None) -> str:
    """The types step's trigger: every input of merge, infer and header publication."""
    from unbake.typemap import facts as source_facts
    from unbake.typemap.abi_facts import refine

    facts = refine(project, refresh_map(project, host))
    return _types_key(project, host, facts, source_facts.published_keys(project, host))


def solve(project: Project, policy: Host | None = None) -> dict[str, Any]:
    """Merge cached per-source facts with the map and infer types; publish the solution."""
    from unbake.typemap import facts as source_facts
    from unbake.typemap import types_db
    from unbake.typemap.abi_facts import refine

    database = types_db.path(project)
    previous = types_db.summary(database) if database.is_file() else {}
    facts = refine(project, refresh_map(project, policy))
    fact_keys = source_facts.published_keys(project, policy)
    result = infer(
        project,
        facts,
        declarations.collect(project, policy, fact_keys),
        cache=None if policy is None else Cache(project.cache),
        shard_dir=project.build / "types",
    )
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
        str(path.relative_to(project.include[0])): sorted(str(home.relative_to(project.include[0])) for home in paths)
        for path, paths in homes.items()
    }
    revision = int(previous.get("revision", 0)) + 1
    result = {
        **storage.identity(project),
        "map_sha256": inputs.digest(project.build / "map/facts.json"),
        "map_shard": facts["shard"],
        "map_shard_sha256": facts["shard_sha256"],
        "abi_supplement": facts.get("abi_supplement"),
        "revision": revision,
        **result,
    }
    from unbake.typemap.database import publish

    publish(project, result, previous, policy=policy)
    result["changes"] = changes(previous, types_db.summary(database))
    return result


def changes(previous: dict[str, Any], current: dict[str, Any], *, shown: int = 10) -> dict[str, Any]:
    """What this solve changed against the last one, per kind: a count and the first names (the solve's
    result). A fixed point needs a pass that changes nothing; a later pass changing names an earlier pass did not
    is oscillation, visible here."""
    found: dict[str, Any] = {}
    for kind in sorted(set(previous) | set(current)):
        before, after = previous.get(kind, {}), current.get(kind, {})
        names = sorted(
            name
            for name in set(before) | set(after)
            if (before.get(name) or {}).get("semantic_sha256") != (after.get(name) or {}).get("semantic_sha256")
        )
        if names:
            found[kind] = {"count": len(names), "first": names[:shown]}
    return found
