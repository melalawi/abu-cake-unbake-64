"""Data ownership derived from the compile: a C unit owns the ROM ranges its object emits as rodata and data."""
from __future__ import annotations

import io
import re
from bisect import bisect_left, bisect_right, insort
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path

from elftools.elf.elffile import ELFFile
from elftools.elf.enums import ENUM_RELOC_TYPE_MIPS

from unbake import config, effort, native, pool, recipes, store, versions
from unbake.contracts import Claim, Finding, Refusal, Snapshot, UnitSpec, digest

_WORD = 0xFFFFFFFF
_MANY = 16  # more equal ranges than this are ambiguous without counting them all
_CODE = digest(Path(__file__).read_bytes())  # cached placements are only valid for the code that made them
def _classes() -> dict[str, str]:
    """Input section name -> output section, for every allocated non-zero section but text (units.toml)."""
    rows = config.load_resource("units.toml")["section"]
    return {name: "." + key for key, row in rows.items() if key != "text" and not row["zero"]
            for name in row["inputs"] if not name.endswith("*")}
def emitted(blob: bytes) -> dict[str, tuple[bytes, dict[int, bool], list[tuple[int, int]]]]:
    """Per output section the bytes ld concatenates in file order, the relocated word offsets (True = to .text) and
    the low-half references from .text as (text offset, offset the instruction adds to the section start)."""
    elf, classes = ELFFile(io.BytesIO(blob)), _classes()
    sections = list(elf.iter_sections())
    relocated: dict[str, dict[int, bool]] = defaultdict(dict)
    lows: dict[str, list[tuple[int, int]]] = defaultdict(list)
    text = next((s.data() for s in sections if s.name == ".text"), b"")
    for section in sections:
        if section["sh_type"] in ("SHT_REL", "SHT_RELA"):
            symbols = sections[section["sh_link"]]
            for r in section.iter_relocations():
                index = symbols.get_symbol(r["r_info_sym"])["st_shndx"]
                target = sections[section["sh_info"]].name
                if isinstance(index, int) and target in classes:
                    relocated[target][r["r_offset"]] = sections[index].name.startswith(".text")
                elif r["r_info_type"] == ENUM_RELOC_TYPE_MIPS["R_MIPS_LO16"] and isinstance(index, int):
                    imm = int.from_bytes(text[r["r_offset"] + 2:r["r_offset"] + 4], "big", signed=True)
                    lows[sections[index].name].append((r["r_offset"], imm))
    out: dict[str, tuple[bytearray, dict[int, bool], list]] = {}
    for section in sections:
        if section.name in classes and section["sh_size"]:
            data, words, refs = out.setdefault(classes[section.name], (bytearray(), {}, []))
            data.extend(bytes(-len(data) % max(section["sh_addralign"], 1)))
            words.update({len(data) + o: t for o, t in relocated[section.name].items()})
            refs.extend((o, len(data) + imm) for o, imm in lows[section.name])
            data.extend(section.data())
    return {c: (bytes(d), w, r) for c, (d, w, r) in out.items()}
def _index(snapshot: Snapshot, version: str) -> tuple:
    def build() -> tuple:
        rom = versions.rom_view(snapshot.versions[version])
        rows = sorted((p.rom_start, p.rom_end, m.name, p.section, p.vram) for m in snapshot.layout.members.values()
                      if m.kind != "function" and sum(q.version == version for q in m.placements) == 1  # a name per row
                      for p in m.placements
                      if p.version == version and p.section in (".rodata", ".data") and p.rom_end > p.rom_start)
        first = defaultdict(list)
        for start, *_ in rows:
            first[int.from_bytes(rom[start:start + 4], "big")].append(start)
        return rom, rows, {r[0]: i for i, r in enumerate(rows)}, first, digest(rows), sorted(
            (r[4], r[4] + r[1] - r[0], r[0], r[1]) for r in rows), snapshot.versions[version].segments
    return effort.memo(("index", snapshot.digest, version), build)
def _same(rom, start: int, data: bytes, words: dict[int, bool], text: int | None) -> bool:
    if not words:
        return rom[start:start + len(data)] == data
    return all(rom[start + o:start + o + 4] == data[o:o + 4] if o not in words else
               not words[o] or text is None or (int.from_bytes(rom[start + o:start + o + 4], "big")
                                                - int.from_bytes(data[o:o + 4], "big") - text) & _WORD == 0
               for o in range(0, len(data), 4))
def _joins(a: tuple, b: tuple) -> bool:  # b follows a in the ROM and in memory
    return b[0] == a[1] and b[4] == a[4] + a[1] - a[0]
def _whole(rows: list, start: int, end: int) -> bool:
    """Whether the rows hold [start, end) without a gap, following each other in the ROM and in memory."""
    at = bisect_right(rows, (start, _WORD)) - 1
    if at < 0 or rows[at][1] <= start:
        return False
    while rows[at][1] < end:
        at += 1
        if at >= len(rows) or not _joins(rows[at - 1], rows[at]):
            return False
    return True
def _loaded(rom, base: int, offset: int) -> int | None:
    """The address the ROM's code forms at a %lo instruction: its low half joined to the lui that feeds its base."""
    word = int.from_bytes(rom[base + offset:base + offset + 4], "big")
    low = (word & 0xFFFF) - ((word & 0x8000) << 1)
    for at in range(offset - 4, max(offset - 132, -4), -4):
        earlier = int.from_bytes(rom[base + at:base + at + 4], "big")
        if earlier >> 26 == 0x0F and (earlier >> 16) & 31 == (word >> 21) & 31:
            return (((earlier & 0xFFFF) << 16) + low) & _WORD
    return None
def _why(rom, data: bytes, words: dict[int, bool], runs: int) -> dict[str, str]:
    """Why bytes that nothing in the ROM matches cannot be placed, and where they do appear."""
    if runs > 1:
        return {"why": f"the code places the bytes in {runs} separate runs of the ROM, not one", "at": ""}
    if words and all(words.values()) and len(words) * 4 >= len(data):
        return {"why": "label table absent from the ROM (every word is a jump into the unit's own text)", "at": ""}
    if not words and (at := rom.find(data)) >= 0:
        return {"why": "bytes found outside the data rows", "at": f"0x{at:X}"}
    return {"why": "bytes found nowhere in the ROM at the unit's text", "at": ""}
def _placed(index: tuple, data: bytes, words: dict[int, bool], refs: list, text: tuple[int, int]) -> int | None:
    """The ROM offset the unit's own code puts its bytes at (the address the ROM's code forms for the first
    reference, less the reference's offset in the section), when the ROM holds the bytes there."""
    rom, _, _, _, _, by_vram, _ = index
    formed = {(a - at) & _WORD for o, at in refs if (a := _loaded(rom, text[1], o)) is not None}
    if len(formed) != 1:
        return None
    start = next(iter(formed))
    i = bisect_right(by_vram, (start, _WORD)) - 1
    if i < 0 or not by_vram[i][0] <= start < by_vram[i][1]:
        return None
    at = by_vram[i][2] + start - by_vram[i][0]
    return at if _same(rom, at, data, words, text[0]) else None
def _content(index: tuple, data: bytes, words: dict[int, bool], vram: int | None) -> list[int]:
    """Every ROM offset that holds the bytes: at a row start when relocated words are involved (a table into the
    unit's own text), anywhere in the data rows otherwise."""
    rom, rows, _, first, *_ = index
    if not data or not rows:
        return []
    if words:
        word = int.from_bytes(data[:4], "big")
        if 0 in words:  # a leading relocated word is predictable only for a jump table into the unit's own text
            starts = first.get((word + vram) & _WORD, []) if words[0] and vram is not None else []
        else:
            starts = first.get(word, [])
        return [s for s in starts if _same(rom, s, data, words, vram)]
    found, at, low, high = [], 0, rows[0][0], max(r[1] for r in rows)
    while len(found) < _MANY and (at := rom.find(data, max(at, low), high)) >= 0:
        found.append(at)
        at += 1
    return found
def locate(index: tuple, blob: bytes, text: tuple[int, int] | None) -> dict[str, list | dict]:
    """Per emitted section the ROM ranges [start, end) that hold its bytes (relocated .text words shifted by the
    unit's text vram), or the reason instead of a list when nothing holds them. Where the unit's own code says where
    the bytes are, that place decides; else every place that holds them, narrowed by the addresses the code forms."""
    rom, *_, segments = index
    vram, base = text or (None, 0)
    out: dict[str, list | dict] = {}
    for section, (data, words, refs) in emitted(blob).items():
        at = _placed(index, data, words, refs, text) if text else None
        starts = [at] if at is not None else _content(index, data, words, vram)
        if len(starts) > 1 and text:
            formed = [(_loaded(rom, base, o), at) for o, at in refs]
            starts = [s for s in starts if all(a is None or a == _vram_of(index, s) + at & _WORD for a, at in formed)
                      ] or starts
        if len(starts) > 1 and text:  # data lies in the segment of the code that uses it
            home = next((lo, hi) for _, lo, hi, _ in segments if lo <= base < hi) if any(
                lo <= base < hi for _, lo, hi, _ in segments) else None
            starts = [s for s in starts if home is None or home[0] <= s < home[1]] or starts
        if len(starts) > 1 and text:  # a pool sits nearer to its code than an equal one of other code does
            near = sorted(abs(s - base) for s in starts)
            starts = [s for s in starts if abs(s - base) == near[0]] if near[0] < near[1] else starts
        runs = len({(a - at) & _WORD for o, at in refs if (a := _loaded(rom, base, o)) is not None}) if text else 0
        out[section] = [[s, s + len(data)] for s in starts] or _why(rom, data, words, runs)
    return out
def _vram_of(index: tuple, offset: int) -> int:
    """The address the rows give a ROM offset (the nearest row at or before it)."""
    rows = index[1]
    i = bisect_right(rows, (offset, _WORD)) - 1
    return rows[i][4] + offset - rows[i][0]
def _job_key(item: tuple[Snapshot, UnitSpec, str | None]) -> str | None:
    """What the unit's placements read: its builds in every holder, the rows they can land on and its own text range."""
    snapshot, unit, only = item
    held = sorted({v for n in unit.members for v in snapshot.layout.members[n].holders()} & ({only} if only else
                  set(snapshot.versions)))
    stamps = [native.stamp(snapshot, unit, v) for v in held]
    return None if None in stamps else digest((stamps, [_index(snapshot, v)[4] for v in held], _CODE))
def _job(item: tuple[Snapshot, UnitSpec, str | None]) -> dict[str, dict | str]:
    snapshot, unit, only = item
    cfg, recipe, out = snapshot.config, recipes.resolve(snapshot.config, unit, {}), {}
    code = replace(unit, members=tuple(n for n in unit.members if snapshot.layout.members[n].kind == "function"))
    for version in sorted({v for n in unit.members for v in snapshot.layout.members[n].holders()}):
        if only not in (None, version):
            continue
        try:
            with store.work(cfg) as work:
                obj = native.objects(snapshot, unit, version, recipe, work)[0]
                blob = obj.read_bytes()
                unresolved = versions.resolve(snapshot.versions[version], versions.undefined(obj), unit.path)
            if unresolved:  # the link would fail: the holder is withheld like any compile failure
                raise Refusal(replace(unresolved[0], reason=f"{unresolved[0].missing[0]} has no address in {version}"
                                      f" ({len(unresolved)} unresolved)"))
            held = next((p for p in native.sections(snapshot, code, version) if p.section == ".text"), None)
        except Refusal as refusal:
            out[version] = f"{refusal.findings[0].key}: {refusal.findings[0].reason}"
            continue
        out[version] = locate(_index(snapshot, version), blob, (held.vram, held.rom_start) if held else None)
    return out
def _range(hits: list, here: list[tuple[int, int]]) -> tuple[list | None, str]:
    """The one range of hits the unit owns, else None and why not. Several ranges that hold the same bytes are
    told apart by the rows the unit owns now."""
    if len(hits) > 1:
        owned = [h for h in hits if any(a < h[1] and h[0] < b for a, b in here)]
        if len(owned) != 1:
            shown = ", ".join(f"0x{s:X}-0x{e:X}" for s, e in hits[:4])
            return None, f"ambiguous among {len(hits)} ranges {shown}"
        return owned[0], ""
    return hits[0], ""
def _named(unit: UnitSpec, picked: dict[str, list[tuple[str, int, int]]], indexes: Mapping[str, tuple],
           names_from: str) -> list[Claim]:
    """The claims of the unit, each named by its section, the unit and the address the first holder gives its start
    (the names_from version where the unit holds it): the one row the claim becomes."""
    out = []
    for section in sorted({s for rows in picked.values() for s, _, _ in rows}):
        held = {v: (a, b) for v, rows in picked.items() for s, a, b in rows if s == section}
        ref = names_from if names_from in held else min(held)
        name = f"{section[1:]}/{Path(unit.path).stem}/{_vram_of(indexes[ref], held[ref][0]):08X}"
        out.extend(Claim(unit.path, v, section, a, b, (name,)) for v, (a, b) in sorted(held.items()))
    return out
def _resolve(unit: UnitSpec, members: Mapping, result: dict, indexes: Mapping[str, tuple], names_from: str
             ) -> tuple[list[str], set[str], list[Claim], list[str]]:
    """The unit's code and bss members, the holders it resolves in, its claims there and the debt it leaves. A holder
    whose data cannot be placed exactly is left out: the unit is withheld there."""
    keep = [n for n in unit.members if members[n].kind == "function"
            or all(p.section == ".bss" for p in members[n].placements)]
    resolved: set[str] = set()
    picked: dict[str, list[tuple[str, int, int]]] = {}
    debt: list[str] = []
    for version, got in result.items():
        if isinstance(got, str):
            debt.append(f"compile failed ({got.split(':')[0]}): {unit.path} {version} {got.split(': ', 1)[1]}")
            continue
        index = indexes[version]
        before = [(p.rom_start, p.rom_end, p.section) for n in unit.members if n not in keep
                  for p in members[n].placements if p.version == version]
        mine: list[tuple[str, int, int]] = []
        for section, hits in got.items():
            here = [(a, b) for a, b, held in before if held == section]
            chosen, why = _range(hits, here) if isinstance(hits, list) else (None, "")
            if chosen is None:
                reason, at = (hits["why"], f" @{hits['at']}" if hits["at"] else "") if isinstance(hits, dict) else (
                    "ambiguous", why[len("ambiguous"):])
                debt.append(f"{reason}: {unit.path} {version} {section}{at}")
                break
            start, end = chosen
            if not _whole(index[1], start, end):
                debt.append(f"bytes are not whole rows that follow each other in memory: {unit.path} {version} "
                            f"{section} @0x{start:X}-0x{end:X}")
                break
            mine.append((section, start, end))
        else:
            resolved.add(version)
            picked[version] = mine
    return keep, resolved, _named(unit, picked, indexes, names_from), debt
def _clash(taken: list[tuple[int, int, str]], start: int, end: int) -> str | None:
    """The unit that holds some of [start, end) in taken (sorted, disjoint), if any."""
    at = bisect_left(taken, (start,))
    return next((o for s, e, o in taken[max(at - 1, 0):at + 1] if s < end and start < e), None)
def _own(unit: UnitSpec, members: Mapping, keep: list[str], resolved: set[str], found: list[Claim],
         taken: dict[str, list[tuple[int, int, str]]], debt: list[str]) -> tuple[UnitSpec, list[Claim]]:
    """The unit with the rows its claims become and the holders it is withheld in, and the claims it keeps. A holder
    claiming bytes that another unit already owns in that version is withheld there: one byte has one owner."""
    holders = {v for n in unit.members for v in members[n].holders()}
    for version in sorted(resolved):
        clashes = (_clash(taken[version], c.start, c.end) for c in found if c.version == version)
        if other := next(filter(None, clashes), None):
            debt.append(f"row owned by another unit: {unit.path} {version} (claimed by {other})")
            resolved.discard(version)
    kept = [c for c in found if c.version in resolved]
    for c in kept:
        insort(taken[c.version], (c.start, c.end, unit.path))
    names = sorted({n for c in kept for n in c.rows})
    return replace(unit, members=(*keep, *names), withheld=tuple(sorted(holders - resolved))), kept
def _findings(debt: dict[str, list[str]], action: str) -> list[Finding]:
    """One finding per reason. Landed C that does not compile in a version it holds is broken, not debt: it refuses."""
    out = []
    for why, rows in sorted(debt.items()):
        broken = why.startswith("compile failed (") and "symbols.unknown" not in why
        key = why[len("compile failed ("):].split(")")[0] if broken else "layout.ownership"
        out.append(Finding(key, f"{len(rows)} unit holders {'do not compile' if broken else 'are withheld'}: {why}",
                           missing=tuple(rows), blocking=broken, action=action if broken else ""))
    return out
def claims(snapshot: Snapshot, version: str | None = None, units: Mapping[str, UnitSpec] | None = None
           ) -> tuple[dict[str, UnitSpec], tuple[Claim, ...], list[Finding]]:
    """The one place that decides who owns which ROM bytes. Every compiled unit's object is followed to the ROM
    ranges its rodata and data occupy in each holder (or the one holder given). Returns the units with the rows
    their claims become and the holders they are withheld in, the claims (unit, version, section, ROM start, ROM end,
    the row they become) and the findings that name what could not be placed or is claimed twice. The rows of the
    split files are generated from the claims alone (layout.claim_rows). Setup, compare, publish and report all
    take ownership from here."""
    with effort.stage("layout.ownership") as span:
        wanted = dict(snapshot.layout.units if units is None else units)
        phases = config.load_resource("units.toml")["kind"]
        todo = sorted((u for u in wanted.values() if "compile" in phases[u.kind]["phases"]), key=_unowned)
        results = pool.map(snapshot.config, "layout.ownership", _job, [(snapshot, u, version) for u in todo], _job_key)
        indexes = {v: _index(snapshot, v) for v in snapshot.versions}
        taken: dict[str, list[tuple[int, int, str]]] = defaultdict(list)
        debt: dict[str, list[str]] = defaultdict(list)
        out, found = dict(wanted), []
        for unit, result in zip(todo, results, strict=True):
            keep, resolved, mine, left = _resolve(unit, snapshot.layout.members, result, indexes,
                                                  snapshot.config.project.names_from)
            out[unit.path], kept = _own(unit, snapshot.layout.members, keep, resolved, mine, taken, left)
            found.extend(kept)
            for line in left:
                debt[line.split(":")[0]].append(line.split(": ", 1)[1])
        findings = _findings(debt, "fix the source of each listed unit")
        span.add(items=len(todo), findings=findings)
        return out, tuple(found), findings
def _unowned(unit: UnitSpec) -> bool:
    """A data unit of rows no function has claimed: it gives them up to the first function whose object emits them."""
    return bool(unit.members) and all(versions.unowned(n) for n in unit.members)
def derive(snapshot: Snapshot, unit: UnitSpec) -> tuple[Snapshot, UnitSpec]:
    """The snapshot and the unit with the data rows its present source emits, per holder, where the ROM holds them
    exactly: what a candidate must reproduce besides its code. The rows its claims become exist only in a private
    copy of the layout, and the returned snapshot is that copy. A unit that is not compiled is returned as it is."""
    if "compile" not in config.load_resource("units.toml")["kind"][unit.kind]["phases"]:
        return snapshot, unit
    if all(snapshot.layout.members[n].kind != "function" for n in unit.members):
        return snapshot, unit  # a data unit's members already are all the data its source emits
    from unbake import layout  # the rows come from the layout, which in turn reads ownership
    members = snapshot.layout.members
    taken: dict[str, list[tuple[int, int, str]]] = defaultdict(list)
    for other in snapshot.layout.units.values():
        data = [members[n] for n in other.members if members[n].kind != "function"
                ] if other.path != unit.path and not _unowned(other) else []
        for p in (p for m in data for p in m.placements if p.rom_end > p.rom_start):
            taken[p.version].append((p.rom_start, p.rom_end, other.path))
    for rows in taken.values():
        rows.sort()
    indexes = {v: _index(snapshot, v) for v in snapshot.versions}
    keep, resolved, mine, debt = _resolve(unit, members, _job((snapshot, unit, None)), indexes,
                                          snapshot.config.project.names_from)
    owned, kept = _own(unit, members, keep, resolved, mine, taken, debt)
    rows = layout.claim_rows(snapshot, kept)
    if rows:
        spans = [(c.version, c.start, c.end) for c in kept]
        units = {path: u for path, u in snapshot.layout.units.items() if not _unowned(u) or not any(
            p.version == v and p.rom_start < e and s < p.rom_end for n in u.members
            for p in members[n].placements for v, s, e in spans)}
        units[owned.path] = owned
        snapshot = layout.overlay(snapshot, {**rows, "layout.toml": layout.dump_map(replace(snapshot.layout,
                                                                                          units=units))})
    return snapshot, owned
def _exact_job(item: tuple[Snapshot, UnitSpec, str]) -> str:
    proofs = native.prove_job(item)
    gap = next((m for p in proofs for m in p.missing), "no measurement")
    return "" if proofs and all(p.exact for p in proofs) else gap
def exact(snapshot: Snapshot, units: dict[str, UnitSpec]) -> tuple[dict[str, UnitSpec], list[Finding]]:
    """A unit is built in a version only where its bytes reproduce the ROM there: a holder that does not is withheld,
    so the Makefile builds exactly the holders that are proven and `make check` has nothing to refuse."""
    with effort.stage("layout.exactness") as span:
        phases = config.load_resource("units.toml")["kind"]
        trial = replace(snapshot, layout=replace(snapshot.layout, units=units), digest=digest((snapshot.digest, units)))
        jobs = [(trial, u, v) for u in units.values() if "compile" in phases[u.kind]["phases"]
                for v in sorted({v for n in u.members for v in snapshot.layout.members[n].holders()} - set(u.withheld))]
        gaps = pool.map(snapshot.config, "layout.exactness", _exact_job, jobs, lambda j: native.stamp(*j))
        lost: dict[str, set[str]] = defaultdict(set)
        debt: dict[str, list[str]] = defaultdict(list)
        for (_, unit, version), gap in zip(jobs, gaps, strict=True):
            if gap:
                lost[unit.path].add(version)
                why = re.sub(r"\d+ bytes differ at \+0x[0-9A-Fa-f]+ ", "bytes differ in ", gap.split(": ", 1)[-1])
                debt[why[:80]].append(f"{unit.path} {version}")
        findings = [Finding("layout.ownership", f"{len(rows)} unit holders are not exact and are withheld: {why}",
                            missing=tuple(rows), blocking=False) for why, rows in sorted(debt.items())]
        span.add(items=len(jobs), findings=findings)
        return {path: replace(u, withheld=tuple(sorted({*u.withheld, *lost.get(path, ())})))
                for path, u in units.items()}, findings
