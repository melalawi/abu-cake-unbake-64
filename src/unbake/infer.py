"""Infer object groups and cross-version symbol addresses."""
from __future__ import annotations

import json
import pickle
import re
import zlib
from collections import Counter, defaultdict
from collections.abc import Sequence
from dataclasses import replace
from itertools import groupby, pairwise
from pathlib import Path
from typing import Any

import rabbitizer

from unbake import config, effort, layout, native, ownership, pool, recipes, store, symbols, versions
from unbake import evidence as readings
from unbake.contracts import Finding, Group, Member, Placement, Plan, Refusal, Snapshot, digest

_IMMEDIATE = frozenset(["lui", "addiu", "ori", "lw", "sw", "lh", "lhu", "sh", "lb", "lbu", "sb", "lwc1", "swc1",
                        "ldc1", "sdc1", "ld", "sd"])
_OPERANDS = re.compile(r"%(?:hi|lo|gp_rel)\((\w+)\)|\bjal\s+(\w+)|\.word\s+(\w+)")
def _placement(snapshot: Snapshot, member: Member) -> Placement:
    held = [p for p in member.placements if p.section == ".text"]
    return next((p for p in held if p.version == snapshot.config.project.names_from), held[0])
def _code(snapshot: Snapshot, p: Placement) -> list[tuple[int, object]]:
    blob = versions.rom_bytes(snapshot.versions[p.version], p.rom_start, p.rom_end)
    return [(w, rabbitizer.Instruction(w, vram=p.vram + i)) for i in range(0, len(blob) - 3, 4)
            for w in [int.from_bytes(blob[i:i + 4], "big")]]
def _masked(code) -> bytes:
    return b"".join((w & (0xFFFF0000 if ins.getOpcodeName() in _IMMEDIATE else
                           0xFC000000 if ins.getOpcodeName() in {"jal", "j"} else
                           0xFFFFFFFF)).to_bytes(4, "big") for w, ins in code)
def _segment(snapshot: Snapshot, p: Placement) -> str:
    return next(name for name, start, end, _ in snapshot.versions[p.version].segments if start <= p.rom_start < end)
_WIDTH = {"lb": 1, "lbu": 1, "sb": 1, "lh": 2, "lhu": 2, "sh": 2, "lw": 4, "sw": 4, "lwc1": 4, "swc1": 4,
          "ld": 8, "sd": 8, "ldc1": 8, "sdc1": 8}
def _scan_job(item) -> list[tuple[str, frozenset[int], frozenset[str], bool, dict[str, list]]]:
    """Per function of one chunk: the rodata addresses it loads, the functions it calls, whether padding ends it and
    its usage: accesses (address, width, mnemonic, how, base), calls (callee, ((argument, value),)), argument
    accesses (argument, offset, width) and steps (base address or "aN", constant) of incremented registers."""
    snapshot, version, rows, addresses, spans = item
    gp = snapshot.versions[version].symbols.get("_gp")
    out = []
    for name, p in rows:
        code = _code(snapshot, p)
        registers = {0: 0, **({28: gp} if gp is not None else {})}
        tags = {28: "full"}  # "hi" after a lui, "full" for an address built on one; read only while a register is known
        args, indexed, stale, pending, pending_target = {4 + n: (n, 0) for n in range(4)}, {}, set(), None, ""
        loads, callees = set(), set()
        use: dict[str, set] = {"accesses": set(), "calls": set(), "args": set(), "steps": set()}
        for i, (w, ins) in enumerate(code):
            op, rs, rt, imm = w >> 26, (w >> 21) & 31, (w >> 16) & 31, w & 65535
            mnemonic, address, signed = ins.getOpcodeName(), None, imm if imm < 32768 else imm - 65536
            dest, width = (w >> 11) & 31 if op == 0 else rt, _WIDTH.get(mnemonic)
            store = bool(width) and mnemonic[0] == "s"
            if width and rs in registers and tags.get(rs) in {"hi", "full"}:
                high = tags[rs] == "hi"
                use["accesses"].add(((registers[rs] + signed) & 0xFFFFFFFF, width, mnemonic,
                                     "abs" if high else "based", None if high else registers[rs]))
            elif width and rs in indexed:
                use["accesses"].add(((indexed[rs] + signed) & 0xFFFFFFFF, width, mnemonic, "indexed", indexed[rs]))
            elif width and rs in args:
                use["args"].add((args[rs][0], args[rs][1] + signed, width))
            if store and rt in registers and tags.get(rt) == "full":
                use["accesses"].add((registers[rt], 4, mnemonic, "taken", None))
            full = rs in registers and tags.get(rs) == "full"
            if mnemonic == "addiu" and rs == rt != 0 and (rs in args or full):
                use["steps"].add((f"a{args[rs][0]}" if rs in args else registers[rs], signed))
            moved = indexing = None
            if op == 0 and w & 63 in {0x21, 0x25}:  # addu or or: a move, or a full address plus an index register
                pairs = ((rs, rt), (rt, rs))
                moved = next((args[r] for r, o in pairs if o == 0 and r in args), None)
                indexing = next((registers[r] for r, o in pairs if tags.get(r) == "full" and r in registers
                                 and o not in registers), None)
            elif mnemonic == "addiu" and rs in args:
                moved = (args[rs][0], args[rs][1] + signed)
            if not (store or op in {1, 2, 3, 4, 5, 6, 7}):
                args.pop(dest, None)
                indexed.pop(dest, None)
                stale.discard(dest)
                if moved:
                    args[dest] = moved
                if indexing is not None:
                    indexed[dest] = indexing
            target = addresses.get(((p.vram + i * 4) & 0xF0000000) | ((w & 0x03FFFFFF) << 2)) if op == 3 else None
            if target not in {None, name}:
                callees.add(target)
            if mnemonic == "lui":
                registers[rt], tags[rt] = imm << 16, "hi"
            elif mnemonic in _IMMEDIATE and rs in registers:
                address = ((registers[rs] | imm) if mnemonic == "ori" else
                           registers[rs] + signed) & 0xFFFFFFFF
                if mnemonic in {"addiu", "ori"}:
                    tags[rt] = "full" if tags.get(rs) in {"hi", "full"} else ""
                    registers[rt] = address
                elif mnemonic.startswith("l"):
                    registers.pop(rt, None)
            elif op not in {2, 3, 4, 5, 6, 7}:
                registers.pop(dest, None)
            registers[0] = 0
            if address is not None and any(a <= address < b for a, b in spans):
                loads.add(address)
            if pending == i:  # the delay slot of a call has run: the argument registers are as the callee sees them
                values = {n: registers[4 + n] for n in range(4) if 4 + n in registers and 4 + n not in stale}
                use["calls"].add((pending_target, tuple(sorted(values.items()))))
                use["accesses"].update((values[n], 0, "jal", "taken", None) for n in values
                                       if tags.get(4 + n) == "full")
                stale.update(range(4, 8))
            if target is not None:
                pending, pending_target = i + 1, target
        words = [w for w, _ in code]
        zeros = len(words) - next((i for i, w in enumerate(reversed(words)) if w), len(words))
        padding = len(words) - zeros - (1 if zeros and words[zeros - 1] == 0x03E00008 else 0)
        out.append((name, frozenset(loads), frozenset(callees), bool(zeros and padding > 0),
                    {key: sorted(found, key=repr) for key, found in use.items()}))
    return out
def _evidence(snapshot, functions, version, cap, scanned):
    held = [(m, p) for m in functions for p in m.placements if p.version == version and p.section == ".text"]
    held.sort(key=lambda row: row[1].rom_start)
    order = {m.name: i for i, (m, _) in enumerate(held)}
    segments = {m.name: _segment(snapshot, p) for m, p in held}
    loads, callers = defaultdict(set), defaultdict(set)
    for name, addresses, callees, *_ in scanned:
        for address in addresses:
            loads[address].add(name)
        for callee in callees:
            callers[callee].add(name)
    padded = {row[0]: row[3] for row in scanned}
    joins, cuts = [], set()
    for names, signal, reach in [(n, "rodata", cap) for n in loads.values() if len(n) > 1] + [
            (n | {callee}, "callee", min(cap, 16)) for callee, n in callers.items()]:
        first, last = min(names, key=order.__getitem__), max(names, key=order.__getitem__)
        if len({segments[n] for n in names}) == 1 and order[last] - order[first] < reach:
            joins.append((first, last, signal))
    for (left, lp), (right, rp) in pairwise(held):
        if (padded[left.name] and lp.rom_end == rp.rom_start and rp.vram % 16 == 0
                and segments[left.name] == segments[right.name]):
            cuts.add((left.name, right.name))
    return order, joins, cuts
def _partition(members, evidence, cap):
    index = {m.name: i for i, m in enumerate(members)}
    joined, cut = [set() for _ in members[1:]], [None] * max(len(members) - 1, 0)
    weight, direct = [0] * len(cut), set()
    for i, (left, right) in enumerate(pairwise(members)):
        if set(right.holders()) != set(evidence) or (i == 0 and set(left.holders()) != set(evidence)):
            joined[i].add("version")
        holding = set(left.holders()) & set(right.holders())
        if holding and all((left.name, right.name) in evidence[v][2] and
                           evidence[v][0][right.name] == evidence[v][0][left.name] + 1 for v in holding):
            cut[i] = "padding"
    for _, joins, _ in evidence.values():
        cover = [0] * len(cut)
        for first, last, signal in joins:
            low, high = sorted((index.get(first, -1), index.get(last, -1)))
            if low >= 0 and not any(cut[low:high]):
                direct.add((low, high))
                for i in range(low, high):
                    joined[i].add(signal)
                    cover[i] += 1
        weight = [max(a, b) for a, b in zip(weight, cover, strict=True)]
    ends = [i for i in range(len(members)) if i == len(cut) or cut[i] or not joined[i] - {"version"}]
    pending = list(zip([0, *(e + 1 for e in ends[:-1])], ends, strict=True))
    while pending:
        low, high = pending.pop()
        if high - low + 1 <= cap or any(a <= low and high <= b for a, b in direct):
            continue
        weakest = min(range(low, high), key=lambda i: (weight[i], abs(2 * i + 1 - low - high), i))
        cut[weakest] = "chain"
        pending.extend(((low, weakest), (weakest + 1, high)))
    ahead = [1] * len(members)
    for i in range(len(cut) - 1, -1, -1):
        if not cut[i] and joined[i] - {"version"}:
            ahead[i] = ahead[i + 1] + 1
    result, current, signals, atom, atom_signals = [], [], set(), [], set()
    for i, m in enumerate(members):
        atom.append(m.name)
        if i < len(cut) and not cut[i] and (joined[i] - {"version"} or
                (joined[i] and len(current) + len(atom) + ahead[i + 1] <= cap)):
            atom_signals.update(joined[i])
            continue
        if current and len(current) + len(atom) > cap:
            result.append((tuple(current), tuple(sorted(signals | {"cap"}))))
            current, signals = [], set()
        current.extend(atom)
        signals.update(atom_signals)
        atom, atom_signals = [], set()
        if i < len(cut) and cut[i]:
            result.append((tuple(current), tuple(sorted(signals | {cut[i]}))))
            current, signals = [], set()
    if current:
        result.append((tuple(current), tuple(sorted(signals))))
    return result
def groups(snapshot: Snapshot) -> tuple[Group, ...]:
    with effort.stage("infer.groups"):
        kept = [g for g in snapshot.layout.groups.values() if g.evidence in {"authored", "proven"}]
        excluded = {n for g in kept for n in g.members}
        functions = [m for m in snapshot.layout.members.values() if m.kind == "function"]
        cap = snapshot.layout.cap
        held = {v: sorted(((p.rom_start, m.name, p) for m in functions for p in m.placements
                           if p.version == v and p.section == ".text"), key=lambda row: row[:2])
            for v in snapshot.versions}
        chunks = []
        for v, rows in held.items():
            addresses = {p.vram: name for _, name, p in rows}
            spans = [(p.vram, p.vram + p.rom_end - p.rom_start) for m in snapshot.layout.members.values()
                     for p in m.placements if p.version == v and p.section == ".rodata"
                     and any(r.start <= p.rom_start < r.end for r in snapshot.config.project.resident[v])]
            chunks += [(snapshot, v, [(name, p) for _, name, p in rows[i:i + 128]], addresses, spans)
                       for i in range(0, len(rows), 128)]
        done = pool.map(snapshot.config, "infer.evidence", _scan_job, chunks, _scan_key)
        scanned = defaultdict(list)
        for chunk, rows in zip(chunks, done, strict=True):
            scanned[chunk[1]].extend(rows)
        evidence = {v: _evidence(snapshot, functions, v, cap, scanned[v]) for v in snapshot.versions}
        source = snapshot.config.project.names_from
        segments = {m.name: _segment(snapshot, _placement(snapshot, m)) for m in functions}
        placed = defaultdict(list)
        for m in sorted(functions, key=lambda m: (segments[m.name], _placement(snapshot, m).vram, m.name)):
            if set(m.holders()) == set(evidence):
                placed[segments[m.name]].append(m)
        for v in [source, *(v for v in evidence if v != source)]:
            inserts = defaultdict(int)
            for m in sorted((m for m in functions if m.name in evidence[v][0]), key=lambda m: evidence[v][0][m.name]):
                segment, line = segments[m.name], placed[segments[m.name]]
                if m not in line:
                    line.insert(inserts[segment], m)
                    inserts[segment] += 1
                else:
                    inserts[segment] = line.index(m) + 1
        result, taken = list(kept), {g.name for g in kept}
        for segment in sorted(placed):
            for protected, run in groupby(placed[segment], key=lambda m: m.name in excluded):
                if protected:
                    continue
                for names, signals in _partition(list(run), evidence, cap):
                    first = _placement(snapshot, snapshot.layout.members[names[0]])
                    base = f"code_{first.vram:08X}"  # one address can open runs in two segments: names stay unique
                    name = next(n for n in (base, *(f"{base}_{i}" for i in range(2, len(taken) + 3))) if n not in taken)
                    taken.add(name)
                    result.append(Group(name, segment, names, "inferred", signals, False))
        return tuple(result)
def sdk(snapshot: Snapshot) -> frozenset[str]:
    with effort.stage("infer.sdk"):
        path = snapshot.config.host.sdk_catalog
        if path is None:
            return frozenset()
        document = json.loads(path.read_bytes())
        config.validate("sdk", document, str(path))
        found = set()
        for m in snapshot.layout.members.values():
            if m.kind != "function":
                continue
            code = _code(snapshot, _placement(snapshot, m))
            masked, matches = _masked(code), 0
            for row in document["signatures"]:
                if "words" in row:
                    words, masks = [int(w, 16) for w in row["words"]], [int(w, 16) for w in row["masks"]]
                    matches += len(code) == len(words) == len(masks) and all(
                        (w & ~mask) == (s & ~mask) for (w, _), s, mask in zip(code, words, masks, strict=True))
                else:
                    matches += (len(masked) == row["size"] and zlib.crc32(masked[:8]) == row["crc_head"]
                                and zlib.crc32(masked) == row["crc_body"])
            if matches == 1:
                found.add(m.name)
        return frozenset(found)
# cached results are only valid for the code that made them
_CODE = digest([Path(f).read_bytes() for f in (__file__, ownership.__file__)])
_PASSES = 3  # rows from claims, claims on those rows, and one pass that proves they agree
def _mask_job(item) -> list[bytes]:
    snapshot, placements = item
    return [_masked(_code(snapshot, p)) for p in placements]
def _scan_key(item) -> str:
    snapshot, version, rows, addresses, spans = item
    return digest((snapshot.versions[version].rom_sha256, snapshot.versions[version].symbols.get("_gp"), rows,
                   addresses, spans, _CODE))
def _mask_key(item) -> str:
    snapshot, placements = item
    return digest((placements, sorted({snapshot.versions[p.version].rom_sha256 for p in placements}), _CODE))
def _masks(snapshot: Snapshot, wanted) -> dict[str, tuple[list[str], list[bytes]]]:
    """Per version the function names in ROM order and their masked code, decoded in the pool in small chunks."""
    held = {v: sorted((p.rom_start, m.name, p) for m in snapshot.layout.members.values() if m.kind == "function"
                      for p in m.placements if p.version == v and p.section == ".text") for v in wanted}
    chunks = [(v, rows[i:i + 128]) for v, rows in held.items() for i in range(0, len(rows), 128)]
    done = pool.map(snapshot.config, "infer.masks", _mask_job,
                    [(snapshot, [p for _, _, p in rows]) for _, rows in chunks], _mask_key)
    out: dict[str, tuple[list[str], list[bytes]]] = {v: ([name for _, name, _ in rows], []) for v, rows in held.items()}
    for (v, _), masks in zip(chunks, done, strict=True):
        out[v][1].extend(masks)
    return out
def _align_job(item) -> dict[str, str]:
    return _align((item[1:3], item[3:]))
def _align(item) -> dict[str, str]:
    """Names of one version mapped to the other's. Equal masked code unique in both versions anchors the order;
    between two anchors the rest pair in order by the share of masked words they hold in common, identical code
    tying by position and any other candidate needing a clear margin."""
    (names_a, masks_a), (names_b, masks_b) = item
    index = [defaultdict(list), defaultdict(list)]
    for table, masks in zip(index, (masks_a, masks_b), strict=True):
        for i, mask in enumerate(masks):
            table[mask].append(i)
    matched = dict(sorted((x[0], index[1][key][0]) for key, x in index[0].items()
                          if len(x) == 1 == len(index[1].get(key, ()))))
    masks, bags = (masks_a, masks_b), {}
    def bag(side: int, i: int) -> Counter:
        return bags.setdefault((side, i), Counter(masks[side][i][o:o + 4] for o in range(0, len(masks[side][i]), 4)))
    def share(i: int, j: int) -> float:
        return sum((bag(0, i) & bag(1, j)).values()) * 4 / max(len(masks_a[i]), len(masks_b[j]))
    for (la, lb), (ra, rb) in pairwise([(-1, -1), *matched.items(), (len(masks_a), len(masks_b))]):
        floor = lb
        for i in range(la + 1, ra):
            scores = sorted(((share(i, j), -j) for j in range(floor + 1, rb)), reverse=True)
            if scores and scores[0][0] >= 0.6 and (len(scores) == 1 or scores[0][0] - scores[1][0] >= 0.15
                                                   or masks_b[-scores[0][1]] == masks_a[i]):
                floor = matched[i] = -scores[0][1]
    return {names_a[i]: names_b[j] for i, j in matched.items()}
def pairs(snapshot: Snapshot, directions: Sequence[tuple[str, str]]) -> dict[tuple[str, str], dict[str, str]]:
    """Per (a, b) direction the functions of version a mapped to their counterparts in b, computed together in the
    pool and cached on both versions' masked code."""
    with effort.stage("infer.pairs"):
        masks = _masks(snapshot, sorted({v for direction in directions for v in direction}))
        found = pool.map(snapshot.config, "infer.align", _align_job,
                         [(snapshot.config, masks[a][0], masks[a][1], masks[b][0], masks[b][1]) for a, b in directions],
                         _align_key)
        return dict(zip(directions, found, strict=True))
def _align_key(item: Any) -> str:
    return digest(item[1:])
def _votes_key(job: Any) -> str:
    return digest((job[0].digest, job[1], job[2], job[3]))
def _votes_job(item) -> Counter:
    snapshot, a, b, rows = item
    counts: Counter = Counter()
    for left, right in rows:
        operands = []
        for v, name in ((a, left), (b, right)):
            path = versions.asm_path(snapshot.config, v, name)
            text = snapshot.read(path.relative_to(snapshot.config.project.root).as_posix()).decode()
            operands.append([next(x for x in match if x) for match in _OPERANDS.findall(text)])
        if len(operands[0]) == len(operands[1]):
            counts.update((x, y) for x, y in zip(*operands, strict=True) if x != y)
    return counts
def _agreed(snapshot: Snapshot, b: str, counts: Counter) -> dict[str, tuple[str, int, int]]:
    targets = defaultdict(set)
    for x, y in counts:
        targets[x].add(y)
    symbols = snapshot.versions[b].symbols
    return {x: (y, symbols[y], n) for (x, y), n in counts.items()
            if len(targets[x]) == 1 and x not in symbols and y in symbols}
def correspondences(snapshot: Snapshot) -> tuple[tuple[str, str, str, str], ...]:
    """(x, a, y, b): the name x that version a's code uses and the name y that version b gives the same thing,
    where every pairing of the two functions agrees."""
    with effort.stage("infer.correspondences"):
        source = snapshot.config.project.names_from
        directions = [(a, b) for v in snapshot.versions if v != source for a, b in ((source, v), (v, source))]
        jobs = [(snapshot, a, b, rows[i:i + 64]) for (a, b), found in pairs(snapshot, directions).items()
                for rows in [list(found.items())] for i in range(0, len(rows), 64)]
        total: dict[tuple[str, str], Counter] = defaultdict(Counter)
        parts = pool.map(snapshot.config, "infer.votes", _votes_job, jobs, _votes_key)
        for (_, a, b, _), part in zip(jobs, parts, strict=True):
            total[a, b].update(part)
        return tuple(sorted((x, a, y, b) for (a, b), merged in total.items()
                            for x, (y, _, _) in _agreed(snapshot, b, merged).items()))
def _tree(snapshot: Snapshot) -> str:
    """The plan compiles every unit, so it depends on every source and header, not only on the layout and symbols."""
    root = snapshot.config.project.root
    return digest([(str(p), p.read_bytes())
                   for place in ("src", "include") for p in sorted((root / place).rglob("*")) if p.is_file()])
def plan(snapshot: Snapshot) -> Plan:
    with effort.stage("infer.plan"):
        key = digest((snapshot.digest, _CODE, _tree(snapshot)))
        return pickle.loads(store.cached(snapshot.config, "infer", key,
                                         lambda: pickle.dumps(_plan(snapshot))))
def _plan(snapshot: Snapshot) -> Plan:
    base, inferred = snapshot.digest, groups(snapshot)
    identified = sdk(snapshot)
    updated = {g.name: g if g.evidence in {"authored", "proven"} else replace(
        g, sdk=bool(g.members) and all(
            n in identified for n in g.members if snapshot.layout.members[n].kind == "function")) for g in inferred}
    updated |= {g.name: replace(g, members=live) for g in snapshot.layout.groups.values()  # data modules outlive it
                if g.name not in updated and (live := tuple(n for n in g.members if n in snapshot.layout.members))
                and all(snapshot.layout.members[n].kind != "function" for n in live)}
    memberships = {n: g.name for g in updated.values() for n in g.members}
    # a unit owns the data it emits
    assigned, claimed, found = ownership.claims(snapshot, units=dict(snapshot.layout.units))
    cut_writes: dict[str, bytes | None] = {}
    for _ in range(_PASSES):  # its rows are generated from its claims: write them now, and plan on the rows they give,
        rows = layout.claim_rows(snapshot, claimed)  # so one commit holds both
        if not rows:
            break
        cut_writes.update(rows)
        snapshot = layout.overlay(snapshot, {**rows, "layout.toml": layout.dump_map(
            replace(snapshot.layout, units=assigned))})
        assigned, claimed, found = ownership.claims(snapshot, units=dict(snapshot.layout.units))
    else:
        raise Refusal(Finding("layout.nonconvergent", "the rows generated from the claims moved the claims again",
                              missing=tuple(sorted(cut_writes)), action="report the units of the files named"))
    if any(f.blocking for f in found):  # landed C that does not compile: nothing else is worth proving
        return Plan("layout", base, {}, (), tuple(f for f in found if f.blocking), (), "infer: refused",
                    digest((base, "refused", found)))
    members = {n: replace(m, group=memberships[n]) if n in memberships else m
               for n, m in snapshot.layout.members.items()}
    units, more = ownership.exact(snapshot, assigned)  # and is built only where its bytes are proven
    debt = tuple(f for f in [*found, *more] if not f.blocking)
    units = {path: replace(u, group=next((memberships[n] for n in u.members if n in memberships), u.group))
             for path, u in units.items()}  # a unit follows its members into the groups inferred now
    value = replace(snapshot.layout, groups=updated, members=members, units=units,
                    digest=digest((updated, members, units)))
    text, writes = layout.dump_map(value), dict(cut_writes)
    if text != snapshot.read("layout.toml"):
        writes["layout.toml"] = text
    sdk_units = sum(g.sdk for g in updated.values())
    message = f"infer: {len(updated)} groups, {sdk_units} sdk units, {len(cut_writes)} files cut"
    return Plan("layout", base, writes, (), (), debt, message, digest((base, writes, message)))

def _evidence_key(item) -> str | None:
    stamp = native.stamp(*item)  # everything the unit's build and the ROM text it reads are
    return None if stamp is None else digest((stamp, _CODE))
def _evidence_job(item) -> dict[str, tuple[set[int], bool]]:
    snapshot, unit, version = item
    members, record = snapshot.layout.members, snapshot.versions[version]
    held = [p for n in unit.members if members[n].kind == "function" for p in members[n].placements
            if p.version == version and p.section == ".text"]
    try:
        with store.work(snapshot.config) as work:
            blob = native.objects(snapshot, unit, version, recipes.resolve(snapshot.config, unit, {}),
                                  work)[0].read_bytes()
    except Refusal:  # a unit that does not compile gives no evidence
        return {}
    first = min(held, key=lambda p: p.rom_start)
    rom = versions.rom_bytes(record, first.rom_start, max(p.rom_end for p in held))
    return readings.read(blob, rom, first.vram)
def addresses(snapshot: Snapshot, debt: Sequence) -> dict[tuple[str, str], tuple[int, bool]]:
    """(name, version) -> (address, is a function) for every name whose address the table lacks or gets wrong
    where the ROM code of the units that use it agrees on one address, the exact units included."""
    with effort.stage("infer.addresses"):
        table = symbols.load(snapshot.read, snapshot.config.project.versions, store.content(snapshot.config).cached)
        units = snapshot.layout.units
        failing = sorted({tuple(row.split(" ", 2)[:2]) for f in debt if "symbols.unknown" in f.reason
                          or "differ in .text" in f.reason for row in f.missing})
        def votes(jobs):
            found: dict[tuple[str, str], list[tuple[set[int], bool]]] = defaultdict(list)
            for (_, _, version), result in zip(jobs, pool.map(snapshot.config, "infer.evidence", _evidence_job, jobs,
                                               _evidence_key),
                                               strict=True):
                for name, row in result.items():
                    found[name, version].append(row)
            return found
        first = votes([(snapshot, units[path], version) for path, version in failing])
        wanted = {key: rows for key, rows in first.items()
                  if len({a for found, _ in rows for a in found}) == 1
                  and table.get(key[0], {}).get(key[1]) not in {a for found, _ in rows for a in found}}
        names = {name for name, _ in wanted}
        failed = set(failing)
        users = [(snapshot, unit, version) for unit in units.values() if unit.path.endswith(".c")
                 for version in sorted({v for n in unit.members for v in snapshot.layout.members[n].holders()}
                                       - set(unit.withheld))
                 if (unit.path, version) not in failed
                 and names & symbols.tokens(snapshot.read(unit.path).decode(errors="replace"))]
        for key, rows in votes(users).items():
            if key in wanted:
                wanted[key] = [*wanted[key], *rows]
        return {key: (next(iter(rows[0][0])), any(call for _, call in rows)) for key, rows in sorted(wanted.items())
                if len({a for found, _ in rows for a in found}) == 1}
