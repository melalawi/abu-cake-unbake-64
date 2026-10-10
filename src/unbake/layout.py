"""Immutable layouts, source placement options, and conservative boundary repair."""
from __future__ import annotations

import bisect
import hashlib
import json
import os
import pickle
import re
import struct
import sys
from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import asdict, replace
from functools import cache, partial
from itertools import chain, pairwise
from pathlib import Path
from typing import Any, cast

from unbake import config as configuration
from unbake import contracts, effort, process, store, symbols
from unbake import versions as version_data
from unbake.contracts import (
    Claim,
    Config,
    Finding,
    Group,
    Json,
    LayoutMap,
    Member,
    Placement,
    Plan,
    Refusal,
    Snapshot,
    UnitSpec,
    Version,
    digest,
)

_LINE = re.compile(
    r"^(\s*-\s*\[\s*)(0[xX][0-9a-fA-F]+|\d+)(\s*,\s*)(\.?\w+)(\s*,\s*)(['\"]?)([^,\]\s'\"]+)(['\"]?)(.*)$" )
_AUTO = re.compile(r"^(func|D)_([0-9A-F]{8})(_\w+)?$")
def _kinds() -> tuple[str, str, str]:
    rows = configuration.load_resource("units.toml")["kind"]
    compiled = next(k for k, r in rows.items() if r["credit"] == "code" and "compile" in r["phases"])
    assembly = next(k for k, r in rows.items() if r["credit"] == "code" and "compile" not in r["phases"])
    datum = next(k for k, r in rows.items() if r["credit"] != "code" and "compile" in r["phases"])
    return compiled, assembly, datum
@cache
def _code() -> str:  # the capture is pickled objects of these modules, so their source is part of its key
    return digest([Path(m.__file__).read_bytes() for m in (contracts, sys.modules[__name__], version_data)])
def _content(config: Config, layout: LayoutMap, vers: Mapping[str, Version], read: Callable[[str], bytes]) -> str:
    """What a snapshot is: the map, the version files, the symbol table and the facts read from extraction. Two
    snapshots with equal content are equal, whichever way they were made (read from disk or proposed by a plan)."""
    return digest((layout.digest, config.digest, _code(), read(symbols.path()),
                   [(v.id, v.rom_sha256, read(v.split), read(v.symbols_file), version_data.facts_digest(v))
                    for v in sorted(vers.values(), key=lambda v: v.id)]))
def _load(config: Config, key: str, produce: Callable[[], bytes]) -> tuple:
    return pickle.loads(store.cached(config, "capture", key, produce))
def _pins(config: Config, paths: list[str]) -> list[tuple[str, str]]:
    """(path, sha256) of each file the capture reads. A file whose stat is unchanged keeps the hash recorded for it, and
    a file rewritten with the same bytes (a regeneration, a touch) keeps the capture warm."""
    base, slot = str(config.project.root), hashlib.sha256(str(config.project.root).encode()).hexdigest()
    blob = store.get(config, "pins", slot)
    known: dict[str, tuple[int, int, str]] = pickle.loads(blob) if blob else {}
    fresh, pins = {}, []
    for p in paths:
        st = os.stat(f"{base}/{p}")
        row = known.get(p)
        if row is None or row[:2] != (st.st_mtime_ns, st.st_size):
            with open(f"{base}/{p}", "rb") as handle:
                row = (st.st_mtime_ns, st.st_size, hashlib.file_digest(handle, "sha256").hexdigest())
        fresh[p] = row
        pins.append((p, row[2]))
    if fresh != known:
        store.put(config, "pins", slot, pickle.dumps(fresh, protocol=5))
    return pins
def capture(config: Config) -> Snapshot:
    with effort.stage("layout.capture"):
        def reader(p: str) -> bytes:
            return (config.project.root / p).read_bytes()
        for _ in range(3):
            commit = process.git(config, "rev-parse", "HEAD").stdout.decode().strip()
            paths = {"layout.toml", symbols.path()}
            for vid, vf in config.project.version_files.items():
                paths.update((vf.split, vf.symbols, vf.baserom))
                paths.update(chain.from_iterable(version_data.fact_files(config, vid)))
            paths.update(p.relative_to(config.project.root).as_posix()
                         for p in config.project.root.glob(".unbake/symbols/*/*.csv"))
            pins = _pins(config, sorted(paths))
            # the root keeps copies of one project apart; not the commit: caches stay warm across setup's own
            key = digest((str(config.project.root), config.digest, pins, _code()))
            def produce():
                vers = version_data.read(config, reader)
                return pickle.dumps((vers, load_map(config, vers, reader("layout.toml"), reader)))
            vers, layout = effort.memo(("capture", key), partial(_load, config, key, produce))
            if commit == process.git(config, "rev-parse", "HEAD").stdout.decode().strip():
                return Snapshot(config, commit, layout, vers, {}, _content(config, layout, vers, reader))
        raise Refusal(Finding("layout.busy", "HEAD moved while reading the project three times",
                              action="retry the command"))
def overlay(snapshot: Snapshot, writes: Mapping[str, bytes | None]) -> Snapshot:
    """The snapshot with the writes laid over it, built once per command however many stages ask for it."""
    return effort.memo(("overlay", snapshot.digest, digest(writes)), lambda: _overlay(snapshot, writes))
def _overlay(snapshot: Snapshot, writes: Mapping[str, bytes | None]) -> Snapshot:
    with effort.stage("layout.overlay"):
        overlays = dict(snapshot.overlays)
        overlays.update(writes)
        files = (p for v in snapshot.versions.values() for p in (v.split, v.symbols_file))
        watched = {"layout.toml", symbols.path(), *files}
        others = sorted((p, hashlib.sha256(b).hexdigest() if b is not None else None)
                        for p, b in overlays.items() if p not in watched)
        new = replace(snapshot, overlays=overlays, digest=digest((snapshot.digest, others)))
        if watched.intersection(writes):
            changed = {v.id for v in snapshot.versions.values() if {v.split, v.symbols_file} & writes.keys()
                       or symbols.path() in writes}
            kept = None if symbols.path() in writes else snapshot.versions  # their facts stand unless the table moved
            vers = {**snapshot.versions, **version_data.read(snapshot.config, new.read, changed, kept)}
            layout = load_map(snapshot.config, vers, new.read("layout.toml"), new.read)
            new = replace(new, versions=vers, layout=layout,
                          digest=digest((_content(snapshot.config, layout, vers, new.read), others)))
        return new
def _rows(snapshot: Snapshot, version: Version) -> list[tuple[str, str, Placement]]:
    return version_data.rows(version, snapshot.read, store.content(snapshot.config).cached)
def load_map(config: Config, versions: Mapping[str, Version], text: bytes, reader: Callable[[str], bytes]) -> LayoutMap:
    """The map of `text` over these versions, built once per command however many overlays ask for it."""
    held = effort.memo(("load-map", config.digest, digest(text), tuple(id(v) for v in versions.values())),
                       lambda: (versions, _load_map(config, versions, text, reader)))  # holds versions: ids stay unique
    return cast(LayoutMap, held[1])  # type: ignore[index]
def _load_map(config: Config, versions: Mapping[str, Version], text: bytes,
              reader: Callable[[str], bytes]) -> LayoutMap:
    with effort.stage("layout.load_map"):
        doc = configuration.toml("layout", text, "layout.toml", "layout.map", store.content(config).cached)
        groups = {r["name"]: Group(**{**r, "members": tuple(r["members"]), "signals": tuple(r["signals"])})
                  for r in doc["group"]}
        collected = {}
        for key in sorted(versions):
            for name, state, placement in version_data.rows(versions[key], reader, store.content(config).cached):
                collected.setdefault(name, []).append((state, placement))
        datum = _kinds()[2]
        members = {}
        group_of = {m: g.name for g in groups.values() for m in g.members}
        for name, rows in collected.items():
            state = next((s for s, p in rows if p.version == config.project.names_from), rows[0][0])
            group = group_of.get(name, "")
            placements = tuple(sorted((p for _, p in rows), key=lambda p: (p.version, p.section)))
            members[name] = Member(name, "function" if any(p.section == ".text" for _, p in rows) else datum,
                                   state, group, placements)
        units = {}
        for row in doc["unit"]:
            unit = UnitSpec(**{**row, "members": tuple(row["members"])})
            missing = tuple(n for n in unit.members if n not in members)
            if missing:
                raise Refusal(Finding("layout.map", reason="unit members are absent from split files",
                                      unit=unit.path, missing=missing))
            units[unit.path] = unit
        fuzzy = {row["member"]: {"path": row["path"], "scores": dict(row["scores"])}
                 for row in doc.get("fuzzy", ())}
        missing = tuple(sorted(set(fuzzy) - members.keys()))
        if missing:
            raise Refusal(Finding("layout.map", reason="fuzzy members are absent from split files", missing=missing))
        return LayoutMap(doc["cap"], groups, members, units, digest(doc), tuple(doc.get("authored", ())), fuzzy)
def _toml(value: Any) -> str:
    return f"[{', '.join(map(_toml, value))}]" if isinstance(value, (list, tuple)) else json.dumps(value)
def dump_map(layout: LayoutMap) -> bytes:
    """Written as text, not through a TOML document model: the map is megabytes and a model walks it for seconds."""
    tables = (("group", sorted(layout.groups.values(), key=lambda g: g.name)),
              ("unit", sorted(layout.units.values(), key=lambda u: u.path)), ("authored", layout.authored))
    out = [f"schema = 3\ncap = {layout.cap}", *(f"{key} = []" for key, rows in tables[:2] if not rows)]
    for key, rows in tables:
        for row in rows:
            values = {k: row[k] for k in ("version", "rom", "kind", "name")} if key == "authored" else asdict(row)
            options = values.pop("options", None)
            out += [f"\n[[{key}]]", *(f"{n} = {_toml(v)}" for n, v in values.items() if n != "withheld" or v)]
            out += [f"\n[{key}.options]", *(f"{k} = {_toml(options[k])}" for k in ("add", "omit"))] if options else []
    for member, row in sorted(layout.fuzzy.items()):
        scores = ", ".join(f"{v} = {_toml(score)}" for v, score in sorted(row["scores"].items()))
        out += ["\n[[fuzzy]]", f"member = {_toml(member)}", f"path = {_toml(row['path'])}", f"scores = {{{scores}}}"]
    return ("\n".join(out) + "\n").encode()
def unit_of(snapshot: Snapshot, member: str) -> UnitSpec | None:
    units = snapshot.layout.units  # the unit holding each member first, indexed once per mapping
    return effort.memo(("owners", id(units)), lambda: (units, {m: u for u in reversed(units.values())
                                                              for m in reversed(u.members)}))[1].get(member)
def asm_unit(snapshot: Snapshot, member: str, version: str) -> UnitSpec:
    with effort.stage("layout.asm_unit"):
        item = snapshot.layout.members.get(member)
        if item is None or not any(p.version == version and p.section == ".text" for p in item.placements):
            raise Refusal(Finding("layout.member", reason="member has no text placement in this version",
                                  unit=member, versions=(version,)))
        config = snapshot.config
        path = version_data.asm_path(config, version, member).relative_to(config.project.root).as_posix()
        return UnitSpec(path, _kinds()[1], item.group, (member,), config.project.toolchain, {"add": [], "omit": []})
def _edit(text: bytes, name: str, *, state: str | None = None, start: int | None = None,
          rename: str | None = None, additions: tuple[tuple[int, str], ...] = (), drop: bool = False,
          types: frozenset[str] | None = None) -> bytes:
    lines = text.decode().splitlines(keepends=True)
    for index, line in enumerate(lines):
        match = _LINE.match(line.rstrip("\r\n"))
        if not match or match[7] != name:
            continue
        ending = line[len(line.rstrip("\r\n")):]
        parts = list(match.groups())
        editable = types if types is not None else configuration.load_resource("units.toml")["section"]["text"]["types"]
        if state is not None and parts[3] not in editable:
            continue
        if state is not None:
            parts[3] = state
        if start is not None:
            parts[1] = f"0x{start:X}" if parts[1].lower().startswith("0x") else str(start)
        if rename is not None:
            parts[6] = rename
        replacement = [] if drop else ["".join(parts) + ending]
        for address, new_name in additions:
            added = parts.copy()
            added[1], added[6] = f"0x{address:X}", new_name
            added[8] = re.sub(r"(\].*?)\s*#.*", r"\1", added[8])
            if replacement and not replacement[-1].endswith("\n"):
                replacement[-1] += "\n"
            replacement.append("".join(added) + ending)
        lines[index:index + 1] = replacement
        return "".join(lines).encode()
    raise Refusal(Finding("layout.member", reason="member has no editable subsegment line", unit=name))
def _claim_row(version: Version, rows: list[list], names: set[str], c: Claim) -> bool:
    """Make the claim's bytes one row named by the claim: the rows it covers are replaced, the row it starts inside
    ends where it starts, and what it leaves of the row it ends inside is a row named by its address. A row is
    [start, end, vram, name, type, name of the line it is written at, whether it takes that line or follows it]."""
    start, end, name = c.start, c.end, c.rows[0]
    at = bisect.bisect_right(rows, start, key=lambda r: r[0]) - 1
    after = bisect.bisect_left(rows, end, key=lambda r: r[0])
    if at < 0 or rows[at][1] <= start:
        raise Refusal(Finding("layout.map", f"claim 0x{start:X}-0x{end:X} of {c.unit} starts outside the data rows",
                              unit=c.unit, versions=(c.version,), path=version.split))
    first, last = rows[at], rows[after - 1]
    kind = first[4] if version_data.section_of(version, first[4], name) == c.section else c.section[1:]
    if after - at == 1 and (first[0], first[1], first[3], first[4]) == (start, end, name, kind):
        return False
    if name in names and name not in {r[3] for r in rows[at:after]}:
        raise Refusal(Finding("layout.map", f"{name} names another row of {version.id}", unit=c.unit,
                              versions=(c.version,), path=version.split))
    new = [start, end, first[2] + start - first[0], name, kind, first[5], first[6] and first[0] == start]
    out = [new]
    if last[1] > end:
        vram = last[2] + end - last[0]
        section = version_data.section_of(version, last[4], name)[1:]
        out.append([end, last[1], vram, f"{section}/unresolved/{vram:08X}", last[4], first[5], False])
    if first[0] < start:
        first[1], at = start, at + 1
    names.difference_update(r[3] for r in rows[at:after])
    names.update(r[3] for r in out)
    rows[at:after] = out
    return True
def _claim_text(text: str, rows: list[list], origin: dict[str, tuple]) -> str:
    """The split file with its data rows as rows has them; the lines of the rows are written the way their own are."""
    lines, group = text.splitlines(keepends=True), {}
    for row in rows:
        group.setdefault(row[5], []).append(row)
    out, found = [], set()
    for line in lines:
        match = _LINE.match(line.rstrip("\r\n"))
        if not match or match[7] not in origin:
            out.append(line)
            continue
        found.add(match[7])
        if out and not out[-1].endswith("\n"):
            out[-1] += "\n"
        for row in group.get(match[7], ()):
            if row[6] and (row[0], row[4], row[3]) == (*origin[match[7]],):
                out.append(line)
                continue
            parts = list(match.groups())
            parts[1] = f"0x{row[0]:X}" if parts[1].lower().startswith("0x") else str(row[0])
            parts[3], parts[6] = row[4], row[3]
            parts[8] = re.sub(r"(\].*?)\s*#.*", r"\1", parts[8])
            out.append("".join(parts) + (line[len(line.rstrip("\r\n")):] or "\n"))
    for name in origin.keys() - found:  # a row written as a mapping has no line to change
        same = [r for r in group.get(name, ()) if r[6] and (r[0], r[4], r[3]) == origin[name]]
        if len(group.get(name, ())) != 1 or not same:
            raise Refusal(Finding("layout.map", "row has no editable subsegment line", unit=name))
    return "".join(out)
def claim_rows(snapshot: Snapshot, claims: Sequence[Claim]) -> dict[str, bytes]:
    """The split files with every claim made one row named by it, whatever rows its bytes were before: the rows of
    the data a unit owns are generated from the claims alone. Nothing changes where the rows are already the claims."""
    with effort.stage("layout.claim_rows"):
        writes = {}
        for vid in sorted({c.version for c in claims}):
            version, text = snapshot.versions[vid], snapshot.read(snapshot.versions[vid].split).decode()
            rows = [[p.rom_start, p.rom_end, p.vram, n, k, n, True] for n, k, p in _rows(snapshot, version)
                    if p.section in (".rodata", ".data")]
            origin, names = {r[3]: (r[0], r[4], r[3]) for r in rows}, {r[3] for r in rows}
            changed = [_claim_row(version, rows, names, c) for c in sorted(
                (c for c in claims if c.version == vid), key=lambda c: c.start)]
            if any(changed):
                writes[version.split] = _claim_text(text, rows, origin).encode()
        return writes
def _grouped(item: Member) -> None:
    if not item.group:
        raise Refusal(Finding("layout.member", reason=f"{item.name} belongs to no group", unit=item.name,
                              action="run unbake setup"))
def unit_options(snapshot: Snapshot, member: str, source: bytes) -> list[tuple[UnitSpec, dict[str, bytes | None]]]:
    return effort.memo(("unit_options", snapshot.digest, member, digest(source)),
                       lambda: _options(snapshot, member, source))
def _options(snapshot: Snapshot, member: str, source: bytes) -> list[tuple[UnitSpec, dict[str, bytes | None]]]:
    with effort.stage("layout.unit_options"):
        if member not in snapshot.layout.members:
            raise Refusal(Finding("layout.member", reason="member is absent from layout", unit=member))
        item = snapshot.layout.members[member]
        compiled, assembly, datum = _kinds()
        if item.kind != "function":
            return [_data_option(snapshot, member, source, datum)]
        _grouped(item)
        split_writes = {}
        holder_rows = {}
        for holder in item.holders():
            version = snapshot.versions[holder]
            rows = _rows(snapshot, version)
            state = next((s for n, s, p in rows if n == member and p.section == ".text"), item.state)
            if state != assembly:
                raise Refusal(Finding("layout.member", reason=f"{member} is {state}, not {assembly}", unit=member))
            holder_rows[holder] = [n for n, _, p in sorted(rows, key=lambda r: r[2].rom_start)]
            split_writes[version.split] = _edit(snapshot.read(version.split), member, state=compiled)
        group_units = sorted((u for u in snapshot.layout.units.values() if u.group == item.group), key=lambda u: u.path)
        options = []
        for unit in group_units:
            if unit.kind != compiled or not unit.members:
                continue
            if all(names.index(member) > 0 and names[names.index(member) - 1] == unit.members[-1]
                   for names in holder_rows.values()):
                options.append((replace(unit, members=(*unit.members, member)),
                                snapshot.read(unit.path) + b"\n" + source))
        group = snapshot.layout.groups.get(item.group)
        stem = item.group if not group_units and group and group.members and group.members[0] == member else member
        standalone = UnitSpec(f"src/{stem}.c", compiled, item.group, (member,),
                              group_units[0].toolchain if group_units else snapshot.config.project.toolchain,
                              {"add": [], "omit": []})
        options.append((standalone, source))
        result = []
        for unit, content in options:
            units = dict(snapshot.layout.units)
            units[unit.path] = unit
            result.append((unit, {**split_writes, unit.path: content,
                                  "layout.toml": dump_map(replace(snapshot.layout, units=units))}))
        return result
def _data_option(snapshot: Snapshot, member: str, source: bytes, kind: str) -> tuple[UnitSpec, dict[str, bytes | None]]:
    """A data member published from C: each holder's split row turns from extracted type to compiled section."""
    item, writes = snapshot.layout.members[member], dict[str, bytes | None]()
    sections = configuration.load_resource("units.toml")["section"]
    raw = frozenset(t for row in sections.values() for t in row["types"] if not t.startswith("."))
    for holder in item.holders():
        version = snapshot.versions[holder]
        state = next((s for n, s, _ in _rows(snapshot, version) if n == member), item.state)
        if state not in raw:
            raise Refusal(Finding("layout.member", reason=f"{member} is {state} in {holder}, not extracted data",
                                  unit=member, versions=(holder,)))
        section = next(name for name, row in sections.items() if state in row["types"])
        writes[version.split] = _edit(writes.get(version.split, snapshot.read(version.split)), member,
                                      state=f".{section}", types=raw)
    stem = member.removesuffix(".c").removeprefix("src/").replace("/", ".")
    group, new = (item.group, None) if item.group else _data_group(snapshot, member)
    unit = UnitSpec(f"src/{stem}.c", kind, group, (member,), snapshot.config.project.toolchain,
                    {"add": [], "omit": []})
    units, groups = {**snapshot.layout.units, unit.path: unit}, dict(snapshot.layout.groups)
    if new:
        groups[group] = new
    return unit, {**writes, unit.path: source,
                  "layout.toml": dump_map(replace(snapshot.layout, units=units, groups=groups))}
def _data_group(snapshot: Snapshot, member: str) -> tuple[str, Group | None]:
    """The module a data item joins: its owner function's when its name says one, else a data module of its own (new
    here): the rows of its section that no function has claimed and that follow each other in the ROM, cut every cap
    rows. A function that later claims a row takes it through the claim path."""
    layout, item = snapshot.layout, snapshot.layout.members[member]
    owner = member.split("/")[1] if member.count("/") == 2 else ""
    if owner in layout.members and layout.members[owner].group:
        return layout.members[owner].group, None
    version = snapshot.versions[item.reference(snapshot.config.project.names_from)]
    section, run = next(p.section for p in item.placements if p.version == version.id), []
    for row in sorted(_rows(snapshot, version), key=lambda r: r[2].rom_start):
        free = row[2].section == section and (row[0] == member or version_data.unowned(row[0]))
        if not (free and run and run[-1][2].rom_end == row[2].rom_start):
            if any(r[0] == member for r in run):
                break
            run = []
        if free:
            run.append(row)
    cut = [r[0] for r in run].index(member) // layout.cap * layout.cap
    chunk = run[cut:cut + layout.cap]
    name = f"data_{chunk[0][2].vram:08X}"
    segment = next(s[0] for s in version.segments if s[1] <= chunk[0][2].rom_start < s[2])
    return name, None if name in layout.groups else Group(name, segment, tuple(r[0] for r in chunk), "inferred",
                                                          ("adjacent",), False)
def _decoded(snapshot: Snapshot, version: Version, rows: list) -> tuple[dict[str, tuple[int, ...]], frozenset[int]]:
    def decode() -> tuple[dict[str, tuple[int, ...]], frozenset[int]]:
        addresses = {p.vram for _, _, p in rows if p.section == ".text"}
        words, references = {}, set()
        for name, _, placement in rows:
            if placement.section not in (".text", ".data", ".rodata"):
                continue
            blob = version_data.rom_bytes(version, placement.rom_start, placement.rom_end)
            values = struct.unpack(f">{len(blob) // 4}I", blob[:len(blob) // 4 * 4])
            if placement.section == ".text":
                words[name] = values
                references.update(((placement.vram + 4 * i + 4) & 0xF0000000) | ((word & 0x3FFFFFF) << 2)
                                  for i, word in enumerate(values) if word >> 26 == 3)
            else:
                references.update(addresses.intersection(values))
        return words, frozenset(references)
    split = hashlib.sha256(snapshot.read(version.split)).hexdigest()
    return effort.memo(("decoded", version.rom_sha256, split), decode)
def _rename(name: str, old: int, new: int) -> str:
    match = _AUTO.fullmatch(name)
    return f"{match[1]}_{new:08X}{match[3] or ''}" if match and int(match[2], 16) == old else name
def boundary_plan(snapshot: Snapshot) -> tuple[Plan, Json]:
    """The plan is a function of the snapshot and this code, so one computation serves every later command."""
    with effort.stage("layout.boundary_plan"):
        key = digest((dump_map(replace(snapshot.layout, fuzzy={})), snapshot.config.digest, snapshot.read(symbols.path()),
                      [(v.id, v.rom_sha256, snapshot.read(v.split), version_data.facts_digest(v))
                       for v in snapshot.versions.values()], _code()))
        return pickle.loads(store.cached(snapshot.config, "boundary", key, lambda: pickle.dumps(_boundary(snapshot))))
def _boundary(snapshot: Snapshot) -> tuple[Plan, Json]:
    assembly = _kinds()[1]
    counts: dict[str, Any] = {rule: {"proposed": 0, "applied": 0, "withheld": 0}
                              for rule in ("prelude", "split", "merge")}
    counts["withheld_reasons"], counts["join"], counts["align"], counts["retype"] = {}, 0, 0, 0
    protected = {row["name"] for row in snapshot.layout.authored}
    candidates, placements = {}, {}
    for holder, version in sorted(snapshot.versions.items()):
        rows = _rows(snapshot, version)
        words, references = _decoded(snapshot, version, rows)
        text_rows = sorted((r for r in rows if r[2].section == ".text"), key=lambda r: r[2].rom_start)
        segment_starts = {start for _, start, _, _ in version.segments}
        linked = [b[2].rom_start == a[2].rom_end and b[2].rom_start not in segment_starts  # b follows a in the ROM
                  for a, b in pairwise(text_rows)]
        for index, (name, state, p) in enumerate(text_rows):
            placements[holder, name] = p
            decisions = []
            previous = text_rows[index - 1] if index and linked[index - 1] else None
            following = text_rows[index + 1] if index < len(linked) and linked[index] else None
            if (state == assembly and name not in protected
                    and all(r is None or r[1] == assembly for r in (previous, following))):
                code = words[name]
                k = next((i for i, word in enumerate(code) if word != 0), len(code))
                if previous and k and k < len(code) and p.vram + 4 * k in references and p.vram not in references:
                    decisions.append(("prelude", (4 * k,)))
                splits = tuple(4 * i for i in range(2, len(code))
                               if p.vram + 4 * i in references and code[i - 2] == 0x03E00008)
                if splits:
                    decisions.append(("split", splits))
                prior = words[previous[0]] if previous else ()
                if (previous and p.vram not in references
                        and (len(prior) < 2 or (prior[-2] != 0x03E00008 and prior[-2] >> 26 != 2))
                        and not any(rule == "prelude" for rule, _ in decisions)):
                    decisions.append(("merge", (0,)))
            candidates.setdefault(name, {})[holder] = tuple(decisions)
    writes, replacements = {}, {}
    table = symbols.edit(snapshot)
    applied = 0
    for name, by_version in candidates.items():
        proposed = {rule for decisions in by_version.values() for rule, _ in decisions}
        if not proposed:
            continue
        decisions = next(iter(by_version.values()))
        agrees = (set(by_version) == set(snapshot.layout.members[name].holders())
                  and all(d == decisions for d in by_version.values()))
        for rule in proposed:
            total = max(len(offsets) for ds in by_version.values() for r, offsets in ds if r == rule)
            counts[rule]["proposed"] += total
            counts[rule]["applied" if agrees else "withheld"] += total
        if not agrees:
            counts["withheld_reasons"]["versions disagree"] = counts["withheld_reasons"].get("versions disagree", 0) + 1
            continue
        preferred = snapshot.config.project.names_from
        reference = preferred if preferred in by_version else sorted(by_version)[0]
        base = placements[reference, name].vram
        shift = next((offsets[0] for rule, offsets in decisions if rule == "prelude"), 0)
        splits = next((offsets for rule, offsets in decisions if rule == "split"), ())
        drop = any(rule == "merge" for rule, _ in decisions)
        renamed = _rename(name, base, base + shift)
        split_names = tuple(_rename(name, base, base + offset) if _AUTO.fullmatch(name)
                            else f"func_{base + offset:08X}" for offset in splits)
        names = split_names if drop else (renamed, *split_names)
        if names != (name,):
            replacements[name] = names
        applied += sum(len(offsets) for _, offsets in decisions)
        if not drop and renamed != name and name in table:
            table[renamed] = {**table.get(renamed, {}), **table.pop(name)}
        for holder in sorted(by_version):
            version, p = snapshot.versions[holder], placements[holder, name]
            cuts = tuple(zip(splits, split_names, strict=True))
            writes[version.split] = _edit(writes.get(version.split, snapshot.read(version.split)), name,
                                          start=p.rom_start + shift if shift else None,
                                          rename=renamed, drop=drop,
                                          additions=tuple((p.rom_start + o, new) for o, new in cuts))
            if drop:
                table.get(name, {}).pop(holder, None)
            else:
                table.setdefault(renamed, {"kind": "function"})[holder] = p.vram + shift
            for offset, new in cuts:
                table.setdefault(new, {"kind": "function"})[holder] = p.vram + offset
        if name in table and drop and table[name].keys() <= {"kind"}:
            del table[name]
    landed = {m for u in snapshot.layout.units.values() for m in u.members}
    data: dict[str, list[tuple[str, str, Placement]]] = {}  # the rows as they will be, misaligned ones cut
    raw = {k: next(t for t in row["types"] if not t.startswith(".")) for k, row in
           configuration.load_resource("units.toml")["section"].items()}
    for holder, version in sorted(snapshot.versions.items()):
        data[holder] = []
        tables = [a for n, a in version.symbols.items() if n.startswith("jtbl_") and n in table]
        for name, was, p in _rows(snapshot, version):
            gap = -p.vram % 4
            cut = p.section in (".data", ".rodata") and version_data.unowned(name) and 0 < gap < p.size
            retype = p.section == ".data" and not was.startswith(".") and version_data.unowned(name) and any(
                p.vram <= a < p.vram + p.size for a in tables)
            state = raw['rodata'] if retype else was  # only read-only data has its jump table words labelled
            data[holder].append((name, state, p))
            if cut or retype:
                fresh = f"{version_data.section_of(version, state, name)[1:]}/unresolved/{p.vram + gap:08X}"
                writes[version.split] = _edit(writes.get(version.split, snapshot.read(version.split)), name,
                                              state=state if retype else None, types=frozenset({was}),
                                              additions=((p.rom_start + gap, fresh),) if cut else ())
            if cut:
                data[holder][-1:] = [(name, state, replace(p, rom_end=p.rom_start + gap)),
                                     (fresh, state, replace(p, rom_start=p.rom_start + gap, vram=p.vram + gap))]
            counts["align"] += cut
            counts["retype"] += retype
    reference, spelled = snapshot.config.project.names_from, defaultdict[str, list[tuple[str, str]]](list)
    for holder in sorted(snapshot.versions, key=lambda v: (v != reference, v)):
        at = {a: n for n, a in snapshot.versions[holder].symbols.items()}
        for name, _, p in data[holder]:
            if p.section in (".data", ".rodata") and (symbol := at.get(p.vram)) in table:  # declared names only
                spelled[symbol].append((holder, name))
    moves = {}
    for shown in spelled.values():  # a landed name is authoritative: it is kept, never renamed, never given a version
        keep = next((n for _, n in shown if n in landed), shown[0][1])
        held_by = snapshot.layout.members[keep].holders() if keep in landed else None
        moves.update({(h, n): keep for h, n in shown
                      if n != keep and n not in landed and (held_by is None or h in held_by)})
    held = {h: {n for n, _, _ in items} for h, items in data.items()}
    while step := [(h, n, k) for (h, n), k in moves.items() if k not in held[h]]:  # a row takes only a free name
        for holder, name, keep in step:
            held[holder] -= {name}
            held[holder] |= {keep}
            del moves[holder, name]
            split = snapshot.versions[holder].split
            with suppress(Refusal):  # a row written as a mapping has no line to rename
                writes[split] = _edit(writes.get(split, snapshot.read(split)), name, rename=keep)
                replacements[name] = (keep,)
                counts["join"] += 1
    # the symbol files follow the table
    wanted = symbols.files(table, {v.id: v.symbols_file for v in snapshot.versions.values()})
    writes.update({path: text for path, text in wanted.items() if text != snapshot.read(path)})
    if replacements:
        def expanded(names):
            return tuple(new for name in names for new in replacements.get(name, (name,)))
        groups = {k: replace(g, members=expanded(g.members)) for k, g in snapshot.layout.groups.items()}
        units = {k: replace(u, members=expanded(u.members))
                 for k, u in snapshot.layout.units.items() if expanded(u.members)}
        fuzzy: dict[str, Json] = {}
        for old, row in snapshot.layout.fuzzy.items():
            fuzzy.setdefault(expanded((old,))[0], {**row, "scores": {}})["scores"].update(row["scores"])
        writes["layout.toml"] = dump_map(replace(snapshot.layout, groups=groups, units=units, fuzzy=fuzzy))
    message = f"layout: {applied + counts['join'] + counts['align'] + counts['retype']} boundary edits"
    return Plan("layout", snapshot.digest, writes, (), (), (), message,
                digest(("layout", snapshot.digest, writes, message))), counts
