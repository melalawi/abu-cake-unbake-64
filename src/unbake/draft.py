"""First-draft C for one function: m2c output with GBI rewrites and advisory hints."""

from __future__ import annotations

import json
import os
import re
from collections import Counter
from collections.abc import Iterator
from pathlib import Path

from unbake import config as configuration
from unbake import effort, gbi, headers, layout, native, pool, process, recipes, store, types, versions
from unbake.contracts import Config, Finding, Json, Member, Refusal, Snapshot, digest

_OPS = {"eq": lambda a, b: a == b, "lt": lambda a, b: a < b, "gte": lambda a, b: a >= b}

def _holds(row: Json, symptoms: Json) -> bool:
    return bool(row["match"]) and all(key in symptoms and all(_OPS[op](symptoms[key], expected)
                                      for op, expected in cond.items()) for key, cond in row["match"].items())

def hints(snapshot: Snapshot, subsystem: str, symptoms: Json) -> tuple[list[Json], list[Json]]:
    rows = list(configuration.load_resource("hints.jsonl")["rows"])
    for number, line in enumerate((snapshot.peek("hints.jsonl") or b"").decode().splitlines(), 1):
        if line.strip():
            row = json.loads(line)
            configuration.validate("hint", row, f"hints.jsonl:{number}")
            rows.append(row)
    matched, others = [], []
    for row in rows:
        if row["subsystem"] not in (subsystem, "any"):
            continue
        ref = {"id": row["id"], "technique": row["technique"], "example": row["example"]}
        if _holds(row, symptoms):
            matched.append({**ref, "because": {k: symptoms[k] for k in row["match"]}})
        else:
            others.append({**ref, "because": {}})
    cap = configuration.load_resource("flow.toml")["packet"]["hints"]
    return matched, others[:cap]

def _m2c_call(item: tuple[Snapshot, str, str, bool, Path]) -> tuple[list[str], str]:
    """m2c's argv and its cache key. The context file is named by its content, so the argv, the assembly and the tool
    are everything m2c reads."""
    snapshot, member, version, stack_structs, context = item
    config = snapshot.config
    unit = layout.unit_of(snapshot, member)
    toolchain = unit.toolchain if unit is not None else config.project.toolchain
    target = configuration.load_resource("toolchains.toml")["toolchain"][toolchain]["m2c"]
    argv = [
        str(process.tool(config, "m2c")), "-t", target, "--valid-syntax", *["--stack-structs"] * stack_structs,
        "--context", str(context), "--function", member,
        str(versions.asm_path(config, version, member)),
    ]
    return argv, digest((argv, Path(argv[-1]).read_bytes(), os.stat(argv[0]).st_mtime_ns))
def _text_key(item: tuple[Snapshot, str, str, bool, Path]) -> str:
    return _m2c_call(item)[1]
def _function_text(item: tuple[Snapshot, str, str, bool, Path]) -> str:
    argv, key = _m2c_call(item)
    config, member, version = item[0].config, item[1], item[2]
    def produce() -> bytes:
        result = process.run("m2c", argv, cwd=config.project.root, tmp=process.scratch(config.project.root))
        if result.exit != 0 or not result.stdout.strip():
            failure = re.findall(r"/\*\s*(Decompilation failure[\s\S]*?)\*/", result.stdout.decode(errors="replace"))
            tail = result.stderr.decode(errors="replace").strip().splitlines()[-3:]
            reason = "; ".join([" ".join(f.split()) for f in failure] or tail)  # m2c prints its failure on stdout
            raise Refusal(Finding("draft.m2c", reason or f"m2c exited {result.exit} with no output", unit=member,
                                  versions=(version,)))
        return result.stdout
    return store.cached(config, "m2c-draft", key, produce).decode(errors="replace")

def _data_text(snapshot: Snapshot, entry: Member, version: str) -> str:
    placements = sorted((p for p in entry.placements if p.version == version and p.section != ".bss"),
                        key=lambda p: p.vram)
    if not placements:
        raise Refusal(Finding("draft.data", "no ROM bytes to draft", unit=entry.name, versions=(version,)))
    record = snapshot.versions[version]
    raw = b"".join(versions.rom_bytes(record, p.rom_start, p.rom_end) for p in placements)
    counts = Counter(record.symbols.values())
    reverse = {vram: name for name, vram in record.symbols.items() if counts[vram] == 1}
    members = snapshot.layout.members
    words = len(raw) % 4 == 0
    referenced: dict[str, str] = {}
    values = []
    if words:
        for index in range(0, len(raw), 4):
            word = int.from_bytes(raw[index:index + 4], "big")
            name = reverse.get(word)
            target = members.get(name) if name else None
            if name is None or name == entry.name or target is None:
                values.append(f"0x{word:08X}")
            else:
                function = target.kind == "function"
                referenced[name] = f"extern void {name}(void);" if function else f"extern unsigned char {name}[];"
                values.append(name if function else f"&{name}")
    else:
        values = [f"0x{byte:02X}" for byte in raw]
    qualifier = "const " if all(p.section == ".rodata" for p in placements) else ""
    kind = "unsigned int" if words else "unsigned char"
    lines = [f"    {', '.join(values[i:i + 8])}," for i in range(0, len(values), 8)]
    head = [referenced[name] for name in sorted(referenced)]
    symbol = reverse.get(placements[0].vram) or f"D_{placements[0].vram:08X}"  # a C name, not the member path
    return "\n".join([*head, f"{qualifier}{kind} {symbol}[] = {{", *lines, "};", ""])

def _text_starts(snapshot: Snapshot) -> dict[tuple[str, int], str]:
    """The first member whose text starts at each (version, ROM offset), built once per command."""
    def build() -> dict[tuple[str, int], str]:
        starts: dict[tuple[str, int], str] = {}
        for name, member in snapshot.layout.members.items():
            for p in member.placements:
                if p.section == ".text":
                    starts.setdefault((p.version, p.rom_start), name)
        return starts
    return effort.memo(("text-starts", snapshot.digest), build)  # type: ignore[return-value]
def split_slot(snapshot: Snapshot, member: str) -> str | None:
    """The member holding the delay slot of `member`'s last instruction when that slot lies outside it."""
    for place in snapshot.layout.members[member].placements:
        version = snapshot.versions[place.version]
        if place.section == ".text" and place.size >= 4 and versions.delay_slot(
                int.from_bytes(versions.rom_bytes(version, place.rom_end - 4, place.rom_end), "big")):
            return _text_starts(snapshot).get((place.version, place.rom_end), "an unowned range")
    return None
def refuse_fragment(snapshot: Snapshot, member: str) -> None:
    successor = split_slot(snapshot, member)
    if successor is not None:
        raise Refusal(Finding("member.split-delay-slot", f"{member} ends on a branch or jump whose delay slot "
                              f"is the first word of {successor}", unit=member))

def _stages(snapshot: Snapshot, member: str, version: str, known: dict[str, str], callees: list[str],
            context: Path) -> Iterator[Json]:
    """Candidate drafts, richest first: m2c with and without stack structs, with and without the GBI rewrites,
    with and without the project headers (m2c's own type definitions can clash with them)."""
    declared = headers.catalog(snapshot, version)
    head = "".join(f"{known[n]}\n" for n in callees if n in known and n not in declared)
    for stack in (True, False):
        raw = re.sub(r"^.*?(\w+)\(.*\);\s*/\* extern \*/\n", lambda m: "" if m[1] in known | declared else m[0],
                     _function_text((snapshot, member, version, stack, context)), flags=re.M)
        for body, rewrites in dict.fromkeys([gbi.rewrite(raw), (raw, 0)]):
            names = set(re.findall(r"\w+", head + body)) & declared.keys()
            paths = sorted({declared[n][0].removeprefix("include/") for n in names})
            for wanted in (paths, []):
                text = configuration.template("m2c_macros.h") + "".join(f'#include "{p}"\n' for p in wanted)
                yield {"text": text + head + body, "gbi_rewrites": rewrites}

def _checked(item: tuple[Snapshot, str, str, Json]) -> Json:
    """A pool job: the draft with why it does not compile under the project's toolchain ("" when it does). Names m2c
    uses but never declares (its stack slots) become M2C_UNK locals and the draft is compiled once more."""
    snapshot, member, version, draft = item
    for attempt in range(2):
        text = draft["text"]
        unit, writes = layout.unit_options(snapshot, member, text.encode())[-1]
        with store.work(snapshot.config) as work:
            proof = native.prove(layout.overlay(snapshot, writes), unit, version,
                                 recipes.resolve(snapshot.config, unit, {}), work)[0]
        error = next((m for m in proof.missing if "compile.error" in m or "preprocess" in m), "")
        names = sorted(set(re.findall(r"`(\w+)' undeclared \(first use in this function", error)))
        opened = re.search(rf"^.*\b{re.escape(member)}\(.*\) \{{\n", text, re.M)
        if attempt or not names or not opened:
            return {**draft, "reason": error}
        slots = [f"{n}[0x100]" if re.search(rf"\b{n}\[", text) else n for n in names]
        text = text[:opened.end()] + f"    M2C_UNK {', '.join(slots)};\n" + text[opened.end():]
        draft = {**draft, "text": text}
    raise AssertionError("unreachable")

def create(snapshot: Snapshot, member: str, out: Path) -> Json:
    """Write the first draft of `member` to `out` and return its report."""
    with effort.stage("draft.create"):
        config = snapshot.config
        entry = snapshot.layout.members.get(member)
        if entry is None or not entry.placements:
            raise Refusal(Finding("land.request", f"{member} is not a member of this layout", unit=member))
        refuse_fragment(snapshot, member)
        version = entry.reference(config.project.names_from)
        group = snapshot.layout.groups.get(entry.group)
        subsystem = group.subsystem if group is not None and entry.group else "unknown"
        known: dict[str, str] = {}
        if entry.kind == "function":
            asm = versions.asm_path(config, version, member).read_text(errors="replace")
            callees = [n for n in dict.fromkeys(re.findall(r"\bjal\s+(\w+)", asm)) if n != member]
            context = headers.context(snapshot, version)  # built once here, never inside each m2c job
            pool.gather(config, [  # m2c on the target, both ways, beside the prototype searches the draft reads
                ("draft.m2c", _function_text, [(snapshot, member, version, s, context) for s in (True, False)],
                 _text_key),
                types.signature_group(snapshot, [member, *callees])])
            known = types.declarations(snapshot, [member, *callees])  # now all cache hits
            stages = list({s["text"]: s for s in _stages(snapshot, member, version, known, callees, context)}.values())
            results = pool.map(config, "draft.compile", _checked, [(snapshot, member, version, s) for s in stages])
            result = next((r for r in results if not r["reason"]), results[0])  # the richest that builds
        else:
            result = {"text": _data_text(snapshot, entry, version), "gbi_rewrites": 0, "reason": ""}
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(result["text"], encoding="utf-8")
        return {"member": member, "kind": entry.kind, "version": version, "path": str(out),
                "gbi_rewrites": result["gbi_rewrites"], "subsystem": subsystem, "compile_error": result["reason"],
                "hints": hints(snapshot, subsystem, {})[1], "declarations": len(known)}

def run(config: Config, params: Json) -> Json:
    """CLI entry: draft params['item'] into .unbake/work."""
    with effort.stage("draft.run"):
        snapshot = layout.capture(config)
        item = params["item"]
        return create(snapshot, item, config.project.root / ".unbake" / "work" / f"{store.stem(item)}.c")
