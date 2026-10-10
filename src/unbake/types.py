"""Persist type evidence and propose declarations from assembly."""

import json
import pickle
import re
from bisect import bisect_right
from collections import defaultdict
from typing import Any

from unbake import config, effort, headers, infer, store
from unbake.contracts import Finding, Json, Refusal, Snapshot, digest


def load(snapshot: Snapshot) -> Json:
    with effort.stage("types.load"):
        raw = snapshot.peek("types.toml")
        if raw is None:
            raise Refusal(Finding("config.missing", "types.toml does not exist", path="types.toml",
                                  action="unbake setup"))
        cached = store.content(snapshot.config).cached
        return effort.memo(("types", digest(raw)), lambda: config.toml("types", raw, "types.toml", cache=cached))
def _dump(doc):  # inline tables written directly: the document is megabytes and a TOML library walks it for seconds
    lines = [f"schema = {doc['schema']}"]
    for kind in ("function", "global", "struct"):
        lines.append(f"\n[{kind}]")
        lines.extend(f"{json.dumps(name)} = {{ " + ", ".join(f"{key} = {json.dumps(value)}"
                     for key, value in sorted(row.items())) + " }" for name, row in sorted(doc[kind].items()))
    return ("\n".join(lines) + "\n").encode()
_KEYWORDS = frozenset(("extern", "const", "volatile", "unsigned", "signed", "char", "short", "int", "long", "float",
                       "double", "void", "struct", "union", "enum"))
def scan(snapshot: Snapshot) -> bytes:
    """Setup's type map: globals from data placements, the prototypes and type definitions the headers declare
    (authored) and the landed definitions. Cached on the inputs it reads."""
    with effort.stage("types.scan"):
        try:
            current = snapshot.read("types.toml")
        except FileNotFoundError:
            load(snapshot)  # refuses by name
            raise
        key = digest((snapshot.layout.digest, snapshot.config.project.names_from, current,
                      [(p, digest(snapshot.read(p))) for p in headers.sources(snapshot)],
                      [(p, digest(snapshot.read(p))) for p in headers.sources(snapshot, "src", ".c")]))
        return store.cached(snapshot.config, "types-scan", key, lambda: _scan(snapshot))
def _scan(snapshot: Snapshot) -> bytes:
    names_from = snapshot.config.project.names_from
    stale = snapshot.read("types.toml")  # read without validation: this scan replaces a file the schema refuses
    schema = int(stale.split(b"=", 1)[1].split(b"\n", 1)[0])
    doc: dict[str, Any] = {"schema": schema, "struct": {}, "global": {}, "function": {}}
    for name, text in headers.landed(snapshot).items():
        doc["function"][name] = {"signature": text, "evidence": "landed"}
    for name, (_, _, text) in headers.catalog(snapshot, names_from).items():
        if re.match(r"(?:typedef|struct|union|enum)\b", text):
            doc["struct"].setdefault(name.rpartition(" ")[2], {"declaration": text, "evidence": "authored"})
        elif re.search(rf"\b{re.escape(name)}\s*\(", text):
            signature = re.sub(r"^extern\s+|\s*;$", "", text)
            doc["function"].setdefault(name, {"signature": signature, "evidence": "authored"})
        else:
            doc["global"][name] = {"declaration": text, "evidence": "authored"}
    known = _KEYWORDS | doc["struct"].keys()
    agreed = {n: text for n, text in headers.disagreements(snapshot, {
        n for n, r in doc["function"].items() if r["evidence"] == "landed"})[0].items()
        if not set(re.findall(r"[A-Za-z_]\w*", text.replace("@", ""))) - known}
    for name, member in sorted(snapshot.layout.members.items()):
        if member.kind == "function" and name in agreed and name not in doc["function"]:
            doc["function"][name] = {"signature": agreed[name].replace("@", name, 1), "evidence": "declared"}
    return _dump(doc)
def conflicts(snapshot: Snapshot) -> list[Finding]:
    """The names the landed C declares with different types and defines nowhere: one finding each, every declaration
    with its file and line. Such a name keeps no declared type until its declarations are one."""
    with effort.stage("types.conflicts"):
        return headers.disagreements(snapshot, {n for n, r in load(snapshot)["function"].items()
                                                if r["evidence"] == "landed"})[1]
def _tables(snapshot: Snapshot, version: str) -> tuple[dict[str, Json], dict[int, list[Any]]]:
    """(uses, readers) of one version, cached: the usage of each function, and per address who touches it."""
    def produce() -> bytes:
        uses = {row[0]: row[4] for row in infer.scan(snapshot)[version]}
        readers = defaultdict(list)
        for name, use in uses.items():
            for address, width, _, how, base in use["accesses"]:
                readers[address].append((name, how, width, base))
        return pickle.dumps((uses, dict(readers)))
    key = digest((snapshot.layout.digest, snapshot.versions[version].rom_sha256, version))
    found = pickle.loads(store.cached(snapshot.config, "usage", key, produce))
    return found  # type: ignore[no-any-return]
def _spans(use: Json) -> list[list[int]]:
    """[start, end, stores, fills] runs of one function's stores and fills, merged across gaps of at most 0x200."""
    taken = {a for a, _, _, how, _ in use["accesses"] if how == "taken"}
    points = [(a, a + w, 1, 0) for a, w, m, how, _ in use["accesses"] if m[0] == "s" and how != "taken"]
    for _, args in use["calls"]:
        given = dict(args)
        if given.get(0) in taken and given.get(2):
            points.append((given[0], given[0] + given[2], 0, 1))
    spans: list[list[int]] = []
    for start, end, stores, fills in sorted(points):
        if spans and start - spans[-1][1] <= 0x200:
            spans[-1] = [spans[-1][0], max(spans[-1][1], end), spans[-1][2] + stores, spans[-1][3] + fills]
        else:
            spans.append([start, end, stores, fills])
    return [s for s in spans if s[2] >= 4 or s[3]]
def _landed(snapshot: Snapshot, name: str) -> bool:
    kind = config.load_resource("units.toml")["kind"].get(snapshot.layout.members[name].state)
    return bool(kind and kind["decompiled"])
def _rows(snapshot: Snapshot, version: str, items: list[tuple[Any, ...]]) -> list[Json]:
    """One row per (address, how, width, base): its names, readers (landed first), spellings, span, base and stride."""
    uses, readers = _tables(snapshot, version)
    symbols, spelled = snapshot.versions[version].symbols, headers.externs(snapshot)
    names = defaultdict(list)
    for symbol, address in symbols.items():
        names[address].append(symbol)
    order, rows = sorted(names), []
    for address, how, width, base in items:
        users = sorted({n for n, *_ in readers.get(address, ())}, key=lambda n: (not _landed(snapshot, n), n))
        span = next(([a, b - a, n, s, f] for n in users for a, b, s, f in _spans(uses[n]) if a <= address < b), None)
        at = bisect_right(order, base) - 1 if base is not None else -1
        stride = next((c for n in users for k, c in uses[n]["steps"] if k == base), None) if base else None
        rows.append({"address": address, "names": sorted(names[address]), "how": how, "width": width,
                     "readers": [(n, _landed(snapshot, n)) for n in users[:5]],
                     "landed_readers": sum(_landed(snapshot, n) for n in users),
                     "spellings": [s for n in names[address] for s in spelled.get(n, ())][:3],
                     "span": dict(zip(("start", "size", "writer", "stores", "fills"), span, strict=True)) if span
                     else None,
                     "base": (names[order[at]][0], base - order[at]) if at >= 0 else None, "stride": stride})
    return rows
def usage(snapshot: Snapshot, member: str, version: str) -> Json:
    """What one function does with memory: per address it touches the row of _rows, its argument accesses and calls."""
    with effort.stage("types.usage"):
        uses, _ = _tables(snapshot, version)
        if member not in uses:
            raise Refusal(Finding("types.usage", f"{member} is no function of {version}", unit=member))
        use = uses[member]
        items = sorted({(a, how, w, base) for a, w, _, how, base in use["accesses"]}, key=lambda r: r[:3])
        callers = [n for n, u in uses.items() if any(c == member for c, _ in u["calls"]) and _landed(snapshot, n)]
        args = {f"a{n}": {"offsets": sorted({o for k, o, _ in use["args"] if k == n}),
                          "stride": next((c for k, c in use["steps"] if k == f"a{n}"), None),
                          "landed_callers": callers[:5]} for n in sorted({k for k, _, _ in use["args"]})}
        return {"addresses": _rows(snapshot, version, items), "args": args,
                "calls": [(c, {f"a{n}": v for n, v in a}) for c, a in use["calls"]]}
def usage_at(snapshot: Snapshot, name: str) -> Json:
    """Per version that names the symbol, the rows of _rows for every address inside the symbol's extent."""
    with effort.stage("types.usage_at"):
        found = {}
        for version, record in snapshot.versions.items():
            if name in record.symbols:
                start = record.symbols[name]
                after = min((a for a in record.symbols.values() if a > start), default=start + 1)
                _, readers = _tables(snapshot, version)
                found[version] = _rows(snapshot, version, [(a, *readers[a][0][1:]) for a in sorted(readers)
                                                           if start <= a < after])
        return found
