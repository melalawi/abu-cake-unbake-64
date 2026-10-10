"""Persist type evidence and propose declarations from assembly."""

import json
import re
from collections.abc import Sequence
from pathlib import Path

from pycparser import CParser, c_ast, c_generator
from pycparser.c_parser import ParseError

from unbake import config, effort, headers, layout, pool, process, store, versions
from unbake.contracts import Finding, Json, Refusal, Snapshot, SourceView, UnitSpec, digest


def load(snapshot: Snapshot) -> Json:
    with effort.stage("types.load"):
        raw = snapshot.peek("types.toml")
        if raw is None:
            raise Refusal(Finding("config.missing", "types.toml does not exist", path="types.toml",
                                  action="unbake setup"))
        return effort.memo(("types", digest(raw)), lambda: config.toml("types", raw, "types.toml"))  # megabytes
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
def _m2c_key(job) -> str:
    _, _, asm, target, _, ctx_digest, tool = job
    return digest((asm, target, ctx_digest, tool))
def _m2c_job(job):
    cfg, name, asm, target, ctx, _, tool = job  # the context travels as a path, never as bytes per job
    asm_path = ctx.parent / f"{digest((name, asm))}.s"
    store.write(asm_path, asm)  # named by its content: a leftover or a concurrent writer holds the same bytes
    result = process.run("types.m2c", [str(tool), "-t", target, "--valid-syntax", "--context", str(ctx),
                                       "--function", name, str(asm_path)], cfg.project.root,
                         tmp=process.scratch(cfg.project.root))
    if result.exit != 0:
        message = result.stderr.decode(errors="replace") or f"m2c exited with {result.exit} (signal {result.signal})"
        return name, "", "", Finding("types.m2c", message, unit=name, blocking=False)
    output = result.stdout.decode(errors="replace")
    signature = next((line.split("{", 1)[0] for line in output.splitlines()
        if line.rstrip().endswith("{") and re.search(r"\b" + re.escape(name) + r"\s*\(", line)), "")
    finding = None if signature else Finding("types.m2c", "m2c produced no function signature",
                                            unit=name, blocking=False)
    return name, " ".join(signature.split()), output, finding
def scan(snapshot: Snapshot) -> bytes:
    """Setup's type map: globals from data placements, the prototypes and type definitions the headers declare
    (authored) and the landed definitions. No m2c runs here. Cached on the inputs it reads."""
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
def signature(snapshot: Snapshot, names: Sequence[str]) -> dict[str, str]:
    """Prototypes for named functions: authored/landed ones from types.toml, the rest from m2c (pool, content cache)."""
    with effort.stage("types.signature") as span:
        cfg, doc = snapshot.config, load(snapshot)
        found = {n: doc["function"][n]["signature"] for n in names if n in doc["function"]}
        toolchains = config.load_resource("toolchains.toml")["toolchain"]
        jobs, contexts, debt = [], {}, []
        for name in dict.fromkeys(names):
            member = snapshot.layout.members.get(name)
            if name in found or member is None or member.kind != "function":
                continue
            version = member.reference(cfg.project.names_from)
            if version not in contexts:
                try:
                    ctx = headers.context(snapshot, version)
                    contexts[version] = ctx, digest(ctx.read_bytes())
                except Refusal as refusal:  # no context: no signatures, never a refusal
                    contexts[version] = None
                    debt.append(Finding("types.m2c", f"no header context for {version}: {refusal.findings[0].reason}",
                                        blocking=False))
            if contexts[version] is None:
                continue
            unit = layout.unit_of(snapshot, name)
            asm = snapshot.read(versions.asm_path(cfg, version, name).relative_to(cfg.project.root).as_posix())
            target = toolchains[unit.toolchain if unit else cfg.project.toolchain]["m2c"]
            jobs.append((cfg, name, asm, target, *contexts[version], process.tool(cfg, "m2c")))
        results = pool.map(cfg, "types.m2c", _m2c_job, jobs, _m2c_key)
        span.add(items=len(jobs), findings=[*debt, *(row[3] for row in results if row[3] is not None)])
        found.update({name: sig for name, sig, _output, _finding in results if sig})
        return found
def context(snapshot: Snapshot) -> str:
    with effort.stage("types.context"):
        doc = load(snapshot)
        return "\n".join([doc["function"][name]["signature"] + ";" for name in sorted(doc["function"])
                          if doc["function"][name]["evidence"] != "authored"] +
            [doc["global"][name]["declaration"] for name in sorted(doc["global"])])
def declarations(snapshot: Snapshot, names: Sequence[str]) -> dict[str, str]:
    with effort.stage("types.declarations"):
        doc = load(snapshot)
        sigs = signature(snapshot, [n for n in names if n not in doc["global"]])
        return {name: sigs[name] + ";" if name in sigs else doc["global"][name]["declaration"] for name in names
                if name in sigs or name in doc["global"]}
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
