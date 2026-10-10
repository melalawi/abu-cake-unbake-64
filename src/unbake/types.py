"""Persist type evidence and propose declarations from assembly."""

import json
import re
from pathlib import Path

from pycparser import CParser, c_ast, c_generator
from pycparser.c_parser import ParseError

from unbake import config, effort, headers, store
from unbake.contracts import Finding, Json, Refusal, Snapshot, SourceView, UnitSpec, digest


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
_CODE = digest(Path(__file__).read_bytes())  # cached scans are only valid for the code that made them
def scan(snapshot: Snapshot) -> bytes:
    """Setup's type map: globals from data placements, the prototypes and type definitions the headers declare
    (authored) and the landed definitions. Cached on the inputs it reads."""
    with effort.stage("types.scan"):
        try:
            current = snapshot.read("types.toml")
        except FileNotFoundError:
            load(snapshot)  # refuses by name
            raise
        key = digest((snapshot.layout.digest, snapshot.config.project.names_from, current, _CODE,
                      [(p, digest(snapshot.read(p))) for p in headers.sources(snapshot)],
                      [(p, digest(snapshot.read(p))) for p in headers.sources(snapshot, "src", ".c")]))
        return store.cached(snapshot.config, "types-scan", key, lambda: _scan(snapshot))
def _scan(snapshot: Snapshot) -> bytes:
    original = load(snapshot)
    names_from = snapshot.config.project.names_from
    doc = {"schema": original["schema"], "struct": {}, "function": {n: r for n, r in original["function"].items()
                                                               if r["evidence"] == "landed"},
           "global": {n: r for n, r in original["global"].items() if r["evidence"] in ("authored", "landed")}}
    for name, (_, _, text) in headers.catalog(snapshot, names_from).items():
        if re.match(r"(?:typedef|struct|union|enum)\b", text):
            doc["struct"].setdefault(name.rpartition(" ")[2], {"declaration": text, "evidence": "authored"})
        elif re.search(rf"\b{re.escape(name)}\s*\(", text):
            signature = re.sub(r"^extern\s+|\s*;$", "", text)
            doc["function"].setdefault(name, {"signature": signature, "evidence": "authored"})
    known = _KEYWORDS | doc["struct"].keys()
    agreed = {n: text for n, text in headers.disagreements(snapshot, {
        n for n, r in doc["function"].items() if r["evidence"] == "landed"})[0].items()
        if not set(re.findall(r"[A-Za-z_]\w*", text.replace("@", ""))) - known}
    for name, member in sorted(snapshot.layout.members.items()):
        if member.kind == "function" and name in agreed and name not in doc["function"]:
            doc["function"][name] = {"signature": agreed[name].replace("@", name, 1), "evidence": "declared"}
        elif member.kind != "function" and name not in doc["global"] and "_padding_" not in name:
            placement = next((p for p in member.placements if p.version == names_from), member.placements[0])
            declared = f"extern {agreed[name].replace('@', name, 1)};" if name in agreed else None
            doc["global"][name] = {"declaration": declared or f"extern u8 {name}[0x{placement.size:X}];",
                "section": placement.section, "size": placement.size, "evidence": "declared" if declared else "splat"}
    return _dump(doc)
def conflicts(snapshot: Snapshot) -> list[Finding]:
    """The names the landed C declares with different types and defines nowhere: one finding each, every declaration
    with its file and line. Such a name keeps no declared type until its declarations are one."""
    with effort.stage("types.conflicts"):
        return headers.disagreements(snapshot, {n for n, r in load(snapshot)["function"].items()
                                                if r["evidence"] == "landed"})[1]
def landed(snapshot: Snapshot, unit: UnitSpec, view: SourceView) -> bytes:
    with effort.stage("types.landed"):
        doc, owners, lines, owner = load(snapshot), [], [], unit.path
        for line in view.text.splitlines(keepends=True):
            marker = re.match(r'^\s*#\s*(?:line\s+)?\d+\s+"([^"]+)"', line)
            if marker:
                owner = marker[1]
            owners.append(owner)
            lines.append("\n" if marker else line)
        from unbake.headers import normal  # headers reads the type map, so it is imported here; one C reading for both
        try:
            tree = CParser().parse(normal("".join(lines)), filename=unit.path)
        except ParseError as error:
            at = re.search(r":(\d+):\d+:", str(error))
            line = int(at[1]) if at and 0 < int(at[1]) <= len(lines) else 0
            where = f" (view line {line} from {owners[line - 1]}: {lines[line - 1].strip()[:160]})" if line else ""
            raise Refusal(Finding("headers.parse", str(error) + where, path=unit.path)) from error
        for node in tree.ext:
            if not isinstance(node, c_ast.FuncDef):
                continue
            path = owners[node.coord.line - 1].removeprefix(snapshot.config.project.root.as_posix() + "/")
            path = re.sub(r"^(?:.*?/)?build/views/[0-9a-fA-F]+/", "", path).removeprefix("./")
            if path == unit.path:
                name = node.decl.name
                doc["function"][name] = {"signature": c_generator.CGenerator().visit(node.decl), "evidence": "landed"}
        return _dump(doc)
