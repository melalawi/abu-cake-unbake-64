"""Evidence closure with explicit unknowns and named incompatible type constraints."""

from __future__ import annotations

from collections import defaultdict, deque
from typing import Any

from unbake.project.config import Held, Policy, Project
from unbake.typemap import declarations, storage
from unbake.typemap.mapping import load_map


class Constraints:
    def __init__(self) -> None:
        self.edges: dict[str, set[str]] = defaultdict(set)
        self.seeds: dict[str, dict[str, list[dict[str, Any]]]] = defaultdict(lambda: defaultdict(list))
        self.users: dict[str, set[str]] = defaultdict(set)
        self.facts: list[dict[str, Any]] = []
        self.resolved: dict[str, dict[str, Any]] = {}

    def touch(self, node: str, user: str | None = None) -> None:
        self.edges[node]
        if user:
            self.users[node].add(user)

    def connect(self, left: str, right: str, evidence: dict[str, Any]) -> None:
        self.edges[left].add(right)
        self.edges[right].add(left)
        provenance = {
            key: evidence[key] for key in ("function", "version", "instruction", "rom_offset") if key in evidence
        }
        self.facts.append({"kind": "value_flow", "left": left, "right": right, "provenance": provenance})

    def seed(self, node: str, type_: str, evidence: dict[str, Any]) -> None:
        self.touch(node)
        if evidence not in self.seeds[node][type_]:
            self.seeds[node][type_].append(evidence)

    def close(self) -> list[dict[str, Any]]:
        visited = set()
        conflicts = []
        self.resolved = {}
        for node in sorted(self.edges):
            if node in visited:
                continue
            members = set()
            pending = deque([node])
            seeds: dict[str, list[dict[str, Any]]] = defaultdict(list)
            users = set()
            while pending:
                current = pending.popleft()
                if current in visited:
                    continue
                visited.add(current)
                members.add(current)
                users.update(self.users[current])
                for type_, provenance in self.seeds[current].items():
                    seeds[type_].extend(p for p in provenance if p not in seeds[type_])
                pending.extend(self.edges[current] - visited)
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


def origin_node(value: dict[str, Any], globals_: dict[str, Any], version: str) -> str | None:
    origins = value.get("origins", [])
    if len(origins) == 1 and origins[0]["offset"] == 0:
        return str(origins[0]["id"])
    constant = value.get("constant")
    names = [
        name for name, row in globals_.items() if row.get("versions", {}).get(version, {}).get("address") == constant
    ]
    return "address:" + names[0] if len(names) == 1 else None


def _merge_records(seeds: list[dict[str, Any]], key: str, graph: Constraints) -> dict[str, Any]:
    records: dict[str, Any] = {}
    for seed in seeds:
        for name, record in seed[key].items():
            previous = records.get(name)
            if previous is not None:
                ignored = ("provenance", "prototype", "declaration", "aliases", "registers")
                old = {k: v for k, v in previous.items() if k not in ignored}
                new = {k: v for k, v in record.items() if k not in ignored}
                if key == "functions":
                    # Parameter names have no type meaning.
                    for row in (old, new):
                        row["params"] = [p["type"] for p in row["params"]]
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
    return records


def infer(project: Project, facts: dict[str, Any], seeds: list[dict[str, Any]]) -> dict[str, Any]:
    graph = Constraints()
    aliases = {name: type_ for seed in seeds for name, type_ in seed["aliases"].items()}
    functions = _merge_records(seeds, "functions", graph)
    globals_ = _merge_records(seeds, "globals", graph)
    structs = _merge_records(seeds, "structs", graph)
    arrays = _merge_records(seeds, "arrays", graph)
    for seed in seeds:
        for name, signature in seed["functions"].items():
            for param, reg in zip(signature["params"], signature["registers"], strict=True):
                if reg is not None:
                    graph.seed(
                        f"param:{name}:{reg}", declarations.canonical(param["type"], aliases), signature["provenance"]
                    )
            type_ = declarations.canonical(signature["return"], aliases)
            register = "f0" if type_ in ("float", "double") else "r2"
            if type_ != "void":
                graph.seed(f"result:{name}:{register}", type_, signature["provenance"])
        for name, record in seed["globals"].items():
            type_ = declarations.canonical(record["type"], aliases)
            graph.seed("global:" + name, type_, record["provenance"])
            graph.seed("address:" + name, type_ + " *", record["provenance"])
    neighbours: dict[str, set[str]] = defaultdict(set)
    fields: dict[str, dict[int, list[dict[str, Any]]]] = defaultdict(lambda: defaultdict(list))
    forwarded: dict[str, set[str]] = defaultdict(set)
    for function, item in facts["functions"].items():
        for version, body in item["versions"].items():
            for reg in body["register_inputs"]:
                graph.touch(f"param:{function}:{reg}", function)
            for call in body["calls"]:
                callee = call["callee"]
                if callee is None:
                    continue
                neighbours[function].add(callee)
                neighbours[callee].add(function)
                signature = functions.get(callee)
                used = (
                    set(signature["registers"])
                    if signature and signature["arity_known"]
                    else {
                        reg
                        for row in facts["functions"].get(callee, {}).get("versions", {}).values()
                        for reg in row["register_inputs"]
                    }
                )
                for reg, value in call["arguments"].items():
                    if reg not in used:
                        continue
                    node = origin_node(value, facts["globals"], version)
                    formal = f"param:{callee}:{reg}"
                    forwarded[formal].add(node if node is not None else "unknown")
                    if node is not None:
                        graph.connect(node, formal, call)
                        graph.touch(node, function)
                        graph.touch(formal, callee)
                index = (call["instruction"] - body["address"]) // 4
                for reg in ("r2", "r3", "f0", "f2"):
                    graph.connect(f"result:{callee}:{reg}", f"return:{function}:{version}:{index}:{reg}", call)
            for returned in body["returns"]:
                for reg, value in returned["values"].items():
                    node = origin_node(value, facts["globals"], version)
                    if node is not None:
                        graph.connect(f"result:{function}:{reg}", node, returned)
                        graph.touch(node, function)
            for memory in body["memory"]:
                index = (memory["instruction"] - body["address"]) // 4
                value_node = f"memory:{function}:{version}:{index}"
                if len(memory.get("symbols", [])) == 1:
                    cell = "global:" + memory["symbols"][0]
                else:
                    origins = memory["base"]["origins"]
                    if len(origins) != 1 or origins[0]["id"].startswith("stack:"):
                        continue
                    origin = origins[0]["id"]
                    offset = origins[0]["offset"] + memory["offset"]
                    cell = f"field:{origin}:{offset}"
                    fields[origin][offset].append(memory)
                    graph.touch(origin, function)
                graph.touch(cell, function)
                if memory["direction"] == "read":
                    graph.connect(cell, value_node, memory)
                else:
                    node = origin_node(memory["value"], facts["globals"], version)
                    if node is not None:
                        graph.connect(cell, node, memory)
                graph.facts.append(
                    {
                        "kind": "access",
                        "node": cell,
                        "width": memory["width"],
                        "signedness": memory["signedness"],
                        "provenance": memory,
                    }
                )

    def common_base(origin: str, active: frozenset[str] = frozenset()) -> str | None:
        if origin == "unknown" or origin in active:
            return None
        sources = forwarded.get(origin, set())
        if not sources:
            return origin
        roots = {common_base(source, active | {origin}) for source in sources}
        return next(iter(roots)) if len(roots) == 1 and None not in roots else None

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
    # Only an explicit pointer declaration supplies field semantics. Width alone is not a seed.
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
            if len(members) == 1 and all(a["width"] == members[0]["size"] and not a["partial"] for a in accesses):
                graph.seed(
                    f"field:{origin}:{offset}",
                    declarations.canonical(members[0]["type"], aliases),
                    record["provenance"],
                )
    conflicts = graph.close()
    conflicts.extend(
        {"key": "types.conflict:" + fact["entity"], **fact}
        for fact in graph.facts
        if fact["kind"] == "declaration_conflict"
    )
    unknown = [row for seed in seeds for row in seed.get("unknown", [])]
    output_functions = {}
    for name in sorted(set(facts["functions"]) | set(functions)):
        signature = functions.get(name)
        params = []
        if signature is not None:
            for param, reg in zip(signature["params"], signature["registers"], strict=True):
                node = f"param:{name}:{reg}"
                state = graph.resolved.get(
                    node,
                    {"state": "known", "type": param["type"], "provenance": [signature["provenance"]], "users": [name]},
                )
                params.append({"name": param["name"], "register": reg, "node": node, **state})
            register = "f0" if declarations.canonical(signature["return"], aliases) in ("float", "double") else "r2"
            returned = (
                {"state": "known", "type": "void", "provenance": [signature["provenance"]], "users": [name]}
                if signature["return"] == "void"
                else graph.resolved[f"result:{name}:{register}"]
            )
            known = (
                signature["arity_known"]
                and not signature.get("declaration_conflict")
                and returned["state"] == "known"
                and all(p["state"] == "known" for p in params)
            )
        else:
            for reg in ("r4", "r5", "r6", "r7", "f12", "f14"):
                node = f"param:{name}:{reg}"
                if node in graph.resolved:
                    params.append({"name": None, "register": reg, "node": node, **graph.resolved[node]})
            returned = graph.resolved.get(
                f"result:{name}:r2", {"state": "unknown", "type": None, "provenance": [], "users": [name]}
            )
            known = False
        output_functions[name] = {
            "state": "known"
            if known
            else "conflict"
            if signature and signature.get("declaration_conflict")
            else "unknown",
            "type": signature["prototype"] if known and signature is not None else None,
            "return": returned,
            "params": params,
            "arity": len(params) if signature and signature["arity_known"] else None,
            "versions": list(facts["functions"].get(name, {}).get("versions", {})),
            "provenance": [signature["provenance"]] if signature else [],
            "prototype": signature["prototype"] if known and signature is not None else None,
        }
        if not known:
            unknown.append(
                f"function:{name}: signature incomplete or conflicting; arity={output_functions[name]['arity']}"
            )
    output_globals = {
        name: {
            **graph.resolved.get("global:" + name, {"state": "unknown", "type": None, "provenance": [], "users": []}),
            "versions": facts["globals"].get(name, {}).get("versions", {}),
            "declaration": globals_.get(name, {}).get("declaration"),
        }
        for name in sorted(set(facts["globals"]) | set(globals_))
    }
    output_structs = {}
    for name, record in structs.items():
        users = sorted(
            {
                user
                for node, state in graph.resolved.items()
                if state["type"] in (record["type"], record["type"] + " *")
                for user in state["users"]
            }
        )
        output_structs[name] = {
            **record,
            "state": "conflict" if record.get("declaration_conflict") else "known",
            "users": users,
        }
    # Advisory partial shapes keep the common base, independent users and unknown extent visible.
    for origin, offsets in shared_fields.items():
        users = sorted({access["function"] for accesses in offsets.values() for access in accesses})
        if len(users) < 2 or graph.resolved.get(origin, {}).get("type"):
            continue
        measured = [
            {
                "offset": offset,
                "widths": sorted({a["width"] for a in accesses}),
                **graph.resolved.get(f"field:{origin}:{offset}", {"state": "unknown", "type": None}),
            }
            for offset, accesses in sorted(offsets.items())
        ]
        name = "Shape_" + storage.digest(origin.encode())[:12]
        output_structs[name] = {
            "state": "unknown",
            "type": None,
            "common_base": origin,
            "users": users,
            "fields": measured,
            "size": None,
            "declaration": None,
            "provenance": [a for rows in offsets.values() for a in rows],
        }
        unknown.append(f"struct:{name}: common base {origin}; size/semantics require proof")
    for name, record in output_globals.items():
        if record["state"] != "known":
            unknown.append(f"global:{name}: {record['state']}")
    dependencies = {name: sorted(neighbours[name]) for name in facts["functions"]}
    for state in graph.resolved.values():
        for user in state["users"]:
            dependencies.setdefault(user, [])
            dependencies[user] = sorted(set(dependencies[user]) | (set(state["users"]) - {user}))
    return {
        "functions": output_functions,
        "globals": output_globals,
        "structs": output_structs,
        "arrays": {
            name: {**row, "state": "known" if row["extent"] is not None else "unknown"} for name, row in arrays.items()
        },
        "constraints": graph.facts,
        "nodes": graph.resolved,
        "conflicts": conflicts,
        "unknown": sorted(set(unknown)),
        "dependencies": dependencies,
    }


def solve(project: Project, policy: Policy | None = None) -> dict[str, Any]:
    facts = load_map(project)
    pinned = storage.inputs(project, headers=True)
    result = infer(project, facts, declarations.collect(project, policy))
    database = project.build / "types/database.json"
    previous = storage.read(database, "types.database") if database.is_file() else {}
    revision = int(previous.get("revision", 0)) + 1
    result = {
        **storage.identity(project),
        "map_sha256": storage.digest((project.build / "map/facts.json").read_bytes()),
        "inputs_sha256": pinned,
        "revision": revision,
        **result,
    }
    if pinned != storage.inputs(project, headers=True):
        raise Held("solve", "types.inputs_stale: declarations changed during solve")
    from unbake.typemap.database import publish

    publish(project, result, previous)
    return result
