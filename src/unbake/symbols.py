"""The project symbol table (symbols.toml): one name per symbol identity, an address per version. The name is a
label only: nothing reads meaning, a version or an address out of it."""
from __future__ import annotations

import re
from collections import Counter
from collections.abc import Callable, Collection, Mapping

from unbake import config, effort
from unbake.contracts import Finding, Refusal, Snapshot

Cached = Callable[[str, str, Callable[[], bytes]], bytes]  # store.content(config).cached
PATH = "symbols.toml"
_PARSED: list = [None, None]  # the last bytes loaded and their table (read-only)
_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_TOKEN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
def path() -> str:
    return PATH
def empty() -> bytes:
    return dump({})
def parse(data: bytes, versions: Collection[str], cache: Cached | None = None) -> dict[str, dict]:
    """The rows of the table: name -> {"kind": function|data, <version>: vram}."""
    document = config.toml("symbols", data, PATH, "symbols.table", cache)
    for name, row in document["symbol"].items():
        stray = sorted(set(row) - {"kind", *versions})
        if stray or not any(v in row for v in versions):
            raise Refusal(Finding("symbols.table", f"{name} must hold an address for a configured version"
                                  + (f"; {stray[0]} is not one" if stray else ""), path=PATH, unit=name))
    return document["symbol"]
def load(reader: Callable[[str], bytes], versions: Collection[str],
         cache: Cached | None = None) -> dict[str, dict]:
    """The table for reading only: parsed once per distinct content."""
    try:
        data = reader(PATH)
    except FileNotFoundError as error:
        raise Refusal(Finding("config.missing", f"{PATH} does not exist", path=PATH, action="unbake setup")) from error
    if _PARSED[0] != data:
        _PARSED[:] = [data, parse(data, versions, cache)]
    return _PARSED[1]
def edit(snapshot: Snapshot) -> dict[str, dict]:
    """A table the caller may change."""
    return parse(snapshot.read(PATH), snapshot.config.project.versions)
def dump(table: Mapping[str, Mapping]) -> bytes:
    lines = ["schema = 1", "", "[symbol]"]
    for name in sorted(table):
        row = table[name]
        lines += [f"\n[symbol.{name}]", f'kind = "{row["kind"]}"']
        lines += [f"{v} = 0x{row[v]:08X}" for v in sorted(row) if v != "kind"]
    return ("\n".join(lines) + "\n").encode()
def declared(table: Mapping[str, Mapping], version: str) -> dict[str, int]:
    return {name: row[version] for name, row in table.items() if version in row}
def render(table: Mapping[str, Mapping], version: str) -> bytes:
    """The splat symbol file of one version: generated from the table, never edited by hand."""
    rows = sorted((row[version], name, row["kind"]) for name, row in table.items() if version in row)
    shared = Counter(address for address, _, _ in rows)
    out = []
    for address, name, kind in rows:
        attrs = [*(["type:func"] if kind == "function" else []),
                 *(["allow_duplicated:true"] if shared[address] > 1 else [])]
        out.append(f"{name} = 0x{address:08X};" + (" // " + " ".join(attrs) if attrs else "") + "\n")
    return "".join(out).encode()
def files(table: Mapping[str, Mapping], paths: Mapping[str, str]) -> dict[str, bytes]:
    """The table and every file generated from it; paths maps a version to its symbol file."""
    return {PATH: dump(table), **{path: render(table, v) for v, path in paths.items()}}
def rename(table: dict[str, dict], old: str, new: str) -> None:
    if old not in table:
        raise Refusal(Finding("symbols.unknown", f"{old} is not a symbol of the project", path=PATH, unit=old))
    if not _NAME.fullmatch(new):
        raise Refusal(Finding("symbols.name", f"{new} is not a C identifier", path=PATH, unit=new))
    if new in table:
        raise Refusal(Finding("symbols.exists", f"{new} already names a symbol", path=PATH, unit=new))
    table[new] = table.pop(old)
def rewrite(text: str, old: str, new: str) -> str:
    """text with every token equal to old replaced; names inside longer identifiers are left alone."""
    return re.sub(rf"(?<![A-Za-z0-9_]){re.escape(old)}(?![A-Za-z0-9_])", new, text)
def tokens(text: str) -> set[str]:
    return set(_TOKEN.findall(text))
def referenced(snapshot: Snapshot, table: Mapping[str, Mapping]) -> set[str]:
    """The table names that landed C sources use."""
    with effort.stage("symbols.referenced"):
        found: set[str] = set()
        for unit in snapshot.layout.units.values():
            if unit.path.endswith(".c"):
                found |= tokens(snapshot.read(unit.path).decode(errors="replace")) & table.keys()
        return found
def join(table: dict[str, dict], a: str, b: str, used: Collection[str], source: str) -> str | None:
    """Make identities a and b one: the name landed C uses survives, else the one with an address in the source
    version. None (nothing changed) when both are used or both place themselves differently in one version."""
    first, second = table[a], table[b]
    if (a in used and b in used) or any(v in first and v in second and first[v] != second[v]
                                        for v in set(first) & set(second) - {"kind"}):
        return None
    keep, drop = (a, b) if a in used or (b not in used and source in first) else (b, a)
    kind = "function" if "function" in (first["kind"], second["kind"]) else "data"
    table[keep] = {**table[drop], **table[keep], "kind": kind}
    del table[drop]
    return keep
def join_pairs(snapshot: Snapshot, table: dict[str, dict], pairs: Collection[tuple[str, str, str, str]]) -> int:
    """Apply infer.correspondences to the table: two identities become one, or an identity gains the address another
    version's code gives the same thing. The number of identities changed."""
    if not pairs:
        return 0
    used, source, changed = referenced(snapshot, table), snapshot.config.project.names_from, 0
    for x, a, y, b in pairs:
        if x in table and y in table and x != y:
            changed += join(table, x, y, used, source) is not None
        elif x in table and y not in table and b not in table[x]:
            table[x][b], changed = snapshot.versions[b].symbols[y], changed + 1
        elif y in table and x not in table and a not in table[y]:
            table[y][a], changed = snapshot.versions[a].symbols[x], changed + 1
    return changed
def fix_rows(table: dict[str, dict], found: Mapping[tuple[str, str], tuple[int, bool]]) -> int:
    """Apply infer.addresses: a name gains (or corrects) the address its version's own code uses. The number changed."""
    for (name, version), (address, function) in found.items():
        table.setdefault(name, {"kind": "function" if function else "data"})[version] = address
    return len(found)
