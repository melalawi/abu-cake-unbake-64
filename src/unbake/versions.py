"""Version facts: ROMs, splat rows, symbol tables, undefined symbols. Never computes an address."""
from __future__ import annotations

import csv
import hashlib
import io
import mmap
import os
import re
import struct
from collections import Counter
from collections.abc import Callable, Collection, Mapping, Sequence
from functools import partial
from glob import glob
from pathlib import Path
from typing import Any

import yaml

from unbake import config as configuration
from unbake import effort, pool, symbols
from unbake.contracts import Config, Finding, Placement, Refusal, Version, digest

_CODE = digest(Path(__file__).read_bytes())  # a parsed asm file is only as true as the reader that parsed it
_SYMBOL = re.compile(r"^\s*([\w.$]+)\s*=\s*(0x[0-9A-Fa-f]+)\s*;")
_ROMS: dict[str, tuple[Any, mmap.mmap]] = {}
def _parsed(path: str, data: bytes, parse: Callable[[bytes], Any]) -> Any:
    """What a parse of the file's bytes gives, once per command."""
    return effort.memo((path, hashlib.sha256(data).hexdigest()), lambda: parse(data))
def _entry(raw: Any) -> dict[str, Any]:
    """Normalise a splat segment or subsegment (list or mapping form) to a mapping."""
    if isinstance(raw, Mapping):
        return dict(raw)
    items = list(raw)
    out: dict[str, Any] = {"start": items[0]}
    if len(items) > 1:
        out["type"] = items[1]
    if len(items) > 2 and isinstance(items[2], str):
        out["name"] = items[2]
    if len(items) > 3 and isinstance(items[3], str):
        out["section"] = items[3]
    return out
def _document(data: bytes, file: str) -> list[dict[str, Any]]:
    try:
        document = yaml.load(data, Loader=yaml.CSafeLoader)
        return [_entry(s) for s in document["segments"]]
    except (yaml.YAMLError, KeyError, TypeError, IndexError) as error:
        raise Refusal(Finding("version.split", reason=f"{file} is not a splat yaml: {error}", path=file)) from error
def _segment_rows(data: bytes) -> tuple[tuple[str, int, int, int], ...]:
    """(name, start, next start, vram or 0) per segment; the last segment ends at its own start (caller widens)."""
    segments = _document(data, "split")
    out = []
    for index, seg in enumerate(segments):
        end = segments[index + 1]["start"] if index + 1 < len(segments) else seg["start"]
        out.append((str(seg.get("name", "")), int(seg["start"]), int(end), int(seg.get("vram", 0) or 0)))
    return tuple(out)
def _symbol_table(data: bytes, file: str) -> dict[str, int]:
    table: dict[str, int] = {}
    for number, line in enumerate(data.decode("utf-8", "replace").splitlines(), 1):
        match = _SYMBOL.match(line)
        if not match:
            continue
        name, address = match.group(1), int(match.group(2), 16)
        if table.setdefault(name, address) != address:
            raise Refusal(
                Finding(
                    "symbols.conflict",
                    reason=f"{name} has two addresses in {file}",
                    path=f"{file}:{number}",
                    line=number,
                    unit=name,
                )
            )
    return table
def _asm_symbols(data: bytes) -> tuple[dict[str, int], dict[str, int], frozenset[str]]:
    text = data.decode(errors="replace")
    label = r"(?m)^\s*(?:([gd])label\s+([\w.$]+)|([\w.$]+):)\s*\n\s*/\*\s*\w+\s+([0-9A-Fa-f]{8})\s"
    jump = r"(?m)/\*\s*\w+\s+([0-9A-Fa-f]{8})\s+([0-9A-Fa-f]{8})\s*\*/\s+j(?:al)?\s+([\w.$]+)\s*$"
    target = {n: (int(v, 16) & 0xF0000000) | ((int(w, 16) & 0x3FFFFFF) << 2) for v, w, n in re.findall(jump, text)}
    highs = {n: int(w, 16) & 0xFFFF for w, n in re.findall(r"(\w{8})\s*\*/\s+lui\s+\S+\s+%hi\(([\w.$]+)\)", text)}
    for w, n in re.findall(r"(\w{8})\s*\*/\s+\w+\s+[^%\n]*%lo\(([\w.$]+)\)", text):
        if n in highs:
            target.setdefault(n, ((highs[n] << 16) + ((int(w, 16) & 0xFFFF) ^ 0x8000) - 0x8000) & 0xFFFFFFFF)
    found = re.findall(label, text)
    branches = re.findall(r"(?m)\*/[ \t]+[bj][a-z]*[ \t]+(?:[^\n,]*,[ \t]*)*([A-Za-z_.][\w.$]*)[ \t]*(?:/\*.*)?$", text)
    code = {n or other for kind, n, other, _ in found if kind == "g" or other}
    code.update(branches, (n for _, _, n in re.findall(jump, text)))
    return {a or b: int(address, 16) for _, a, b, address in found}, target, frozenset(code)
def fact_files(config: Config, vid: str) -> tuple[list[str], list[str]]:
    root = config.project.root
    text = (root / config.project.version_files[vid].split).read_text()
    keys = ("undefined_funcs_auto_path", "undefined_syms_auto_path")
    paths = (re.search(rf"^\s+{key}:\s*(\S+)", text, re.M) for key in keys)
    autos = [p for m in paths if m and (root / (p := m[1].strip("'\""))).is_file()]
    base = str(root)
    found = [f"{top}/{name}"[len(base) + 1:] for top, _, names in os.walk(f"{base}/asm/{vid}")
             for name in names if name.endswith(".s")]
    return autos, sorted(found)
def _asm_job(item: tuple[str, str]):
    root, rel = item
    return _asm_symbols((Path(root) / rel).read_bytes())
def _asm_key(item: tuple[str, str]) -> str:
    """A splat asm file is rewritten only when its bytes change, so name, size and mtime stand for its content."""
    stat = os.stat(f"{item[0]}/{item[1]}")
    return digest((item[1], stat.st_size, stat.st_mtime_ns, _CODE))
def _generated(config: Config, vid: str, files: tuple[list[str], list[str]], parsed: Sequence[Any], declared
               ) -> tuple[dict[str, int], frozenset[str]]:
    root, facts, code = config.project.root, {}, set()
    autos, sources = files
    for path in autos:
        found = _parsed(path, (root / path).read_bytes(), partial(_symbol_table, file=path))
        facts.update(found)
        code.update(found if "funcs" in Path(path).name else ())
    contexts = sorted((root / ".unbake" / "symbols" / vid).glob("spim_context*.csv"))
    rows = (r for p in contexts for r in csv.DictReader(io.StringIO(p.read_text())))
    facts.update({r["getName"]: int(r["address"], 16) for r in rows if r["category"] == "symbol"})
    for rel, (labels, jumps, called) in zip(sources, parsed, strict=True):
        code |= called
        facts.update({n: a for n, a in jumps.items() if n not in declared and n not in facts})
        for name, address in ({} if contexts else labels).items():
            if facts.get(name, address) != address:
                raise Refusal(Finding("symbols.conflict", f"{name}: generated addresses disagree", path=rel))
            if name not in declared:
                facts[name] = address
    return facts, frozenset(code)
def facts_digest(version: Version) -> str:
    """A version's symbol table and code names as one hash (tens of thousands of names), computed once per command."""
    return effort.memo(("facts", id(version)), lambda: (version, hashlib.sha256(
        repr((sorted(version.symbols.items()), sorted(version.code))).encode()).hexdigest()))[1]
def _rom_sha(rom: Path) -> str:
    stat = rom.stat()
    return effort.memo(("rom", str(rom), stat.st_mtime_ns, stat.st_size),
                       lambda: hashlib.sha256(rom.read_bytes()).hexdigest())
def read(config: Config, reader: Callable[[str], bytes], only: Collection[str] | None = None) -> dict[str, Version]:
    with effort.stage("versions.read"):
        root, table = config.project.root, config.project.version_files
        named = symbols.load(reader, table)
        wanted = [v for v in table if only is None or v in only]
        files = {v: effort.memo(("facts", str(root), v), lambda v=v: fact_files(config, v)) for v in wanted}
        def stamps() -> list[tuple[str, int]]:  # tens of thousands of stats: one pass per command, not one per read
            paths = [*(p for v in wanted for p in (*files[v][0], *files[v][1])),
                     *(p.relative_to(root) for p in sorted((root / ".unbake" / "symbols").glob("*/*.csv")))]
            return [(str(p), os.stat(root / p).st_mtime_ns) for p in paths]
        key = digest((str(root), wanted, [(reader(table[v].split), reader(table[v].symbols)) for v in wanted],
                      reader(symbols.path()), effort.memo(("stamps", str(root), tuple(wanted)), stamps)))
        def compute() -> dict[str, Version]:
            items = [(str(root), rel) for v in wanted for rel in files[v][1]]
            parsed = iter(pool.map(config, "versions.asm", _asm_job, items, _asm_key))
            out: dict[str, Version] = {}
            for vid in wanted:
                declared = symbols.declared(named, vid)
                facts, code = _generated(config, vid, files[vid], [next(parsed) for _ in files[vid][1]], declared)
                row, rom = table[vid], root / table[vid].baserom
                segments = _parsed(row.split, reader(row.split), _segment_rows)
                if segments:  # the last segment runs to the end of the ROM
                    name, start, _, vram = segments[-1]
                    segments = (*segments[:-1], (name, start, max(start, rom.stat().st_size), vram))
                out[vid] = Version(vid, rom, _rom_sha(rom), row.split, row.symbols, {**facts, **declared}, segments,
                                   code)
            return out
        return dict(effort.memo(("versions.read", key), compute))
def section_of(version: Version, kind: str, where: str) -> str:
    section = next(("." + key for key, row in configuration.load_resource("units.toml")["section"].items()
                    if kind in row["types"]), None)
    if section is None:
        raise Refusal(Finding("version.split", reason=f"{where} has unknown type {kind!r}", path=version.split))
    return section
def rows(version: Version, reader: Callable[[str], bytes]) -> list[tuple[str, str, Placement]]:
    with effort.stage("versions.rows") as span:
        segments = _document(reader(version.split), version.split)
        ends = {name: end for name, _, end, _ in version.segments}
        out: list[tuple[str, str, Placement]] = []
        for seg in segments:
            if "type" not in seg:
                continue
            seg_name, seg_start = str(seg.get("name", "")), int(seg["start"])
            seg_end, seg_vram = ends.get(seg_name, seg_start), int(seg.get("vram", 0) or 0)
            subs = [_entry(s) for s in seg.get("subsegments", ())]
            end_markers = [int(s["start"]) for s in subs if "type" not in s and s.get("start") is not None]
            subs = [s for s in subs if "type" in s]
            if not subs:
                subs = [dict(seg)]
            for index, sub in enumerate(subs):
                start = int(sub["start"]) if sub.get("start") is not None else seg_end
                next_start = subs[index + 1].get("start") if index + 1 < len(subs) else None
                end = int(next_start) if next_start is not None else min(
                    (e for e in end_markers if e >= start), default=seg_end)
                where = f"{seg_name} subsegment {index} at 0x{start:X}"
                if "name" not in sub:
                    reason = f"{where} has no name"
                    raise Refusal(Finding("version.split", reason=reason, path=version.split, unit=seg_name))
                kind = str(sub["type"])
                section = section_of(version, str(sub.get("section", kind)), where)
                vram = int(sub["vram"]) if "vram" in sub else seg_vram + (start - seg_start)
                size = 0
                if section == ".bss":
                    next_sub = subs[index + 1] if index + 1 < len(subs) else {}
                    next_vram = next_sub.get("vram", seg.get("vram_end"))
                    size = int(sub.get("size", int(next_vram) - vram if next_vram is not None else end - start))
                    if size < 0:
                        raise Refusal(Finding("version.split", "negative bss extent", path=version.split))
                    end = start
                out.append((str(sub["name"]), kind, Placement(version.id, section, start, end, vram, size)))
        out.sort(key=lambda r: (r[2].rom_start, r[2].vram, r[0]))
        twice = next((n for n, c in Counter(r[0] for r in out).items() if c > 1), None)
        if twice is not None:  # one name is one row: a second row of that name would be silently left unowned
            at = [f"0x{r[2].rom_start:X}" for r in out if r[0] == twice]
            raise Refusal(Finding("version.split", reason=f"{twice} names two rows of {version.id} ({', '.join(at)})",
                                  path=version.split, unit=twice))
        span.add(items=len(out))
        return out
def delay_slot(word: int) -> bool:  # a MIPS jump or branch: the next word is its delay slot
    op, rt = word >> 26, word >> 16 & 31
    return ((op == 0 and word & 62 == 8) or (op == 1 and rt & 12 == 0) or 2 <= op <= 7 or 20 <= op <= 23
            or (16 <= op <= 18 and word >> 21 & 31 == 8))
def rom_view(version: Version) -> mmap.mmap:
    key = str(version.rom)
    if key not in _ROMS:
        handle = version.rom.open("rb")
        _ROMS[key] = (handle, mmap.mmap(handle.fileno(), 0, access=mmap.ACCESS_READ))
    return _ROMS[key][1]
def rom_bytes(version: Version, start: int, end: int) -> bytes:
    view = rom_view(version)
    if start < 0 or end < start or end > len(view):
        raise Refusal(Finding("version.rom", reason=f"range 0x{start:X}-0x{end:X} is outside {version.id}'s ROM "
                              f"of {len(view)} bytes", versions=(version.id,), path=str(version.rom)))
    return bytes(view[start:end])
def asm_path(config: Config, version: str, member: str) -> Path:
    with effort.stage("versions.asm_path"):
        root = config.project.root
        found = effort.memo(("asm", str(root), version), lambda: {Path(p).stem: Path(p) for p in sorted(
            glob(str(root / "asm" / version / "**" / "*.s"), recursive=True))}).get(member)
        if found is None:
            raise Refusal(Finding("version.split", reason=f"no assembly for {member} in {version}", unit=member,
                                  versions=(version,), action="run unbake setup"))
        return found
def undefined(obj: Path) -> tuple[str, ...]:
    """The global and weak names an object leaves undefined, read straight from its big-endian ELF32 symbol table."""
    with effort.stage("versions.undefined"):
        data = obj.read_bytes()
        if data[:6] != b"\x7fELF\x01\x02":
            raise Refusal(Finding("native.exit", reason="the object is not a 32-bit big-endian ELF file",
                                  path=str(obj)))
        shoff, = struct.unpack_from(">I", data, 32)
        shentsize, shnum = struct.unpack_from(">HH", data, 46)
        names: set[str] = set()
        for i in range(shnum):
            _, kind, _, _, offset, size, link, _, _, entsize = struct.unpack_from(">10I", data, shoff + i * shentsize)
            if kind != 2:  # SHT_SYMTAB
                continue
            strings = struct.unpack_from(">10I", data, shoff + link * shentsize)[4]
            for at in range(offset + entsize, offset + size, entsize):  # entry 0 is the null symbol
                name, _, _, info, _, index = struct.unpack_from(">IIIBBH", data, at)
                if index == 0 and name and info >> 4 in (1, 2):  # undefined, global or weak
                    names.add(data[strings + name:data.index(b"\0", strings + name)].decode())
        return tuple(sorted(names))
def resolve(version: Version, names: Sequence[str], unit: str) -> tuple[Finding, ...]:
    return tuple(
        Finding(
            "symbols.unknown",
            reason=f"{name} has no address in {version.id}",
            versions=(version.id,),
            missing=(name,),
            unit=unit,
            path=symbols.path(),
            action="add the row to symbols.toml or run unbake setup to join identities",
        )
        for name in names
        if name not in version.symbols
    )
