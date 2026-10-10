"""First-draft C for one function: m2c output with GBI rewrites and advisory hints."""

from __future__ import annotations

import json
import os
import re
from collections import Counter
from pathlib import Path

from unbake import config as configuration
from unbake import effort, gbi, headers, layout, process, store, types, versions
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

def _function_text(snapshot: Snapshot, member: str, version: str) -> tuple[str, int]:
    config = snapshot.config
    unit = layout.unit_of(snapshot, member)
    toolchain = unit.toolchain if unit is not None else config.project.toolchain
    target = configuration.load_resource("toolchains.toml")["toolchain"][toolchain]["m2c"]
    argv = [
        str(process.tool(config, "m2c")), "-t", target, "--valid-syntax", "--stack-structs",
        "--context", str(headers.context(snapshot, version)), "--function", member,
        str(versions.asm_path(config, version, member)),
    ]
    def produce() -> bytes:
        result = process.run("m2c", argv, cwd=config.project.root, tmp=process.scratch(config.project.root))
        if result.exit != 0 or not result.stdout.strip():
            failure = re.findall(r"/\*\s*(Decompilation failure[\s\S]*?)\*/", result.stdout.decode(errors="replace"))
            tail = result.stderr.decode(errors="replace").strip().splitlines()[-3:]
            reason = "; ".join([" ".join(f.split()) for f in failure] or tail)  # m2c prints its failure on stdout
            raise Refusal(Finding("draft.m2c", reason or f"m2c exited {result.exit} with no output", unit=member,
                                  versions=(version,)))
        return result.stdout
    # the context file is named by its content, so the argv, the assembly and the tool are everything m2c reads
    key = digest((argv, Path(argv[-1]).read_bytes(), os.stat(argv[0]).st_mtime_ns))
    return gbi.rewrite(store.cached(config, "m2c-draft", key, produce).decode(errors="replace"))

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

def split_slot(snapshot: Snapshot, member: str) -> str | None:
    """The member holding the delay slot of `member`'s last instruction when that slot lies outside it."""
    for place in snapshot.layout.members[member].placements:
        version = snapshot.versions[place.version]
        if place.section == ".text" and place.size >= 4 and versions.delay_slot(
                int.from_bytes(versions.rom_bytes(version, place.rom_end - 4, place.rom_end), "big")):
            return next((n for n, m in snapshot.layout.members.items() if any(
                p.version == place.version and p.section == ".text" and p.rom_start == place.rom_end
                for p in m.placements)), "an unowned range")
    return None
def refuse_fragment(snapshot: Snapshot, member: str) -> None:
    successor = split_slot(snapshot, member)
    if successor is not None:
        raise Refusal(Finding("member.split-delay-slot", f"{member} ends on a branch or jump whose delay slot "
                              f"is the first word of {successor}", unit=member))

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
            text, rewrites = _function_text(snapshot, member, version)
            asm = versions.asm_path(config, version, member).read_text(errors="replace")
            callees = [n for n in dict.fromkeys(re.findall(r"\bjal\s+(\w+)", asm)) if n != member]
            known = types.declarations(snapshot, [member, *callees])  # on demand, for the target and its callees
            text = "".join(f"{known[n]}\n" for n in callees if n in known) + text
        else:
            text, rewrites = _data_text(snapshot, entry, version), 0
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text, encoding="utf-8")
        return {"member": member, "kind": entry.kind, "version": version, "path": str(out),
                "gbi_rewrites": rewrites, "subsystem": subsystem,
                "hints": hints(snapshot, subsystem, {})[1], "declarations": len(known)}

def run(config: Config, params: Json) -> Json:
    """CLI entry: draft params['item'] into .unbake/work."""
    with effort.stage("draft.run"):
        snapshot = layout.capture(config)
        item = params["item"]
        return create(snapshot, item, config.project.root / ".unbake" / "work" / f"{store.stem(item)}.c")
