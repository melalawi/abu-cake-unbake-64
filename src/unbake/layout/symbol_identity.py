"""Symbol correspondence independent of executable body equality."""

from __future__ import annotations

from bisect import bisect_right
from collections import defaultdict
from collections.abc import Callable, Mapping
from itertools import combinations, pairwise
from typing import Any

from unbake.layout import split
from unbake.layout.rodata_references import collect, words
from unbake.project.flow import Span
from unbake.project.rom import Rom

Key = tuple[str, int]


def local_switches(image: bytes, f: split.Function, spans: list[Span]) -> dict[int, dict[str, Any]]:
    """Prove the guarded sll/lui/addu/lw/jr form and every bounded table word."""
    code = words(image[f.start : f.end])
    refs, _ = collect(f.name, image[f.start : f.end], None, None)
    tables = {r.offset // 4: r.address for r in refs if r.type == "indexed" and r.scale == 4 and r.opcode == 35}
    result = {}
    for at, table in tables.items():
        jump = at + 1
        if at < 6 or jump >= len(code):
            continue
        guard, branch, delay, shift, high, add, load, transfer = code[at - 6 : jump + 1]
        count, index, flag = guard & 65535, guard >> 21 & 31, guard >> 16 & 31
        scaled, base = shift >> 11 & 31, high >> 16 & 31
        target = ((branch & 65535) ^ 0x8000) - 0x8000 + at - 4
        if (
            guard >> 26 != 11
            or not 0 < count <= 512
            or not flag
            or branch >> 26 != 4
            or {branch >> 21 & 31, branch >> 16 & 31} != {flag, 0}
            or not jump < target < len(code)
            or shift >> 26 != 0
            or shift & 63 != 0
            or shift >> 6 & 31 != 2
            or shift >> 16 & 31 != index
            or not scaled
            or high >> 26 != 15
            or not base
            or base == scaled
            or add >> 26 != 0
            or add & 63 not in (32, 33)
            or add >> 11 & 31 != base
            or {add >> 21 & 31, add >> 16 & 31} != {base, scaled}
            or load >> 26 != 35
            or load >> 21 & 31 != base
            or transfer >> 26 != 0
            or transfer & 63 != 8
            or transfer >> 21 & 31 != load >> 16 & 31
        ):
            continue
        # The delay slot must not change the bounded index before scaling it.
        destination = delay >> 11 & 31 if delay >> 26 == 0 else delay >> 16 & 31
        if (destination == index and index != 0) or delay >> 26 in (1, 2, 3, 4, 5, 6, 7, 20, 21, 22, 23):
            continue
        placements = []
        for span in spans:
            if (
                span["address"] is None
                or not span["address"] <= table < table + count * 4 <= span["address"] + span["end"] - span["start"]
            ):
                continue
            start = span["start"] + table - span["address"]
            if not 0 <= start < start + count * 4 <= len(image):
                continue
            values = words(image[start : start + count * 4])
            for entry_bias in (0, 0x80000000):
                targets = [(value + entry_bias) & 0xFFFFFFFF for value in values]
                if all(value % 4 == 0 and f.address <= value < f.address + f.end - f.start for value in targets):
                    placements.append(
                        {
                            "rom_start": start,
                            "count": count,
                            "targets": targets,
                            "entry_bias": entry_bias,
                            "address": table,
                        }
                    )
        if len(placements) == 1:
            result[jump] = placements[0]
    return result


def graph(
    images: Mapping[str, bytes | Rom],
    inventories: dict[str, list[split.Function]],
    details: dict[Key, dict[str, Any]] | None = None,
    loaded_spans: Mapping[str, list[Span]] | None = None,
) -> tuple[dict[Key, set[Key]], dict[Key, set[Key]], dict[Key, str]]:
    """Resolve direct calls and tail transfers only at unambiguous entries."""
    outgoing: dict[Key, set[Key]] = defaultdict(set)
    incoming: dict[Key, set[Key]] = defaultdict(set)
    unresolved: dict[Key, str] = {}
    for version, ff in inventories.items():
        spans = loaded_spans.get(version, []) if loaded_spans else []
        entries: dict[int, list[split.Function]] = defaultdict(list)
        for f in ff:
            entries[f.address].append(f)
        by_address = sorted(ff, key=lambda f: f.address)
        addresses = [f.address for f in by_address]
        maximum_ends = []
        maximum = 0
        for f in by_address:
            maximum = max(maximum, f.address + f.end - f.start)
            maximum_ends.append(maximum)
        cartridge = images[version]
        image = cartridge if isinstance(cartridge, bytes) else cartridge.image()
        for f in ff:
            key = (version, f.start)
            code = words(image[f.start : f.end])
            switches = (
                local_switches(image, f, spans)
                if any(word >> 26 == 0 and word & 63 == 8 and word >> 21 & 31 != 31 for word in code)
                else {}
            )
            if switches and details is not None:
                details.setdefault(key, {})["local_switches"] = switches
            for index, word in enumerate(code):
                opcode = word >> 26
                if opcode == 0 and word & 63 in (8, 9):
                    # Returns are not edges. Other register transfers have no
                    # proved target; do not infer an indirect call's identity.
                    if index not in switches and (word >> 21 & 31 != 31 or word & 63 == 9):
                        unresolved[key] = "symbol-graph-indirect"
                    continue
                if opcode not in (1, 2, 3, 4, 5, 6, 7, 20, 21, 22, 23):
                    continue
                pc = f.address + 4 * index
                target = (
                    ((pc + 4) & 0xF0000000) | ((word & 0x03FFFFFF) << 2)
                    if opcode in (2, 3)
                    else (pc + 4 + (((word & 65535) ^ 0x8000) - 0x8000) * 4) & 0xFFFFFFFF
                )
                if opcode != 3 and f.address <= target < f.address + f.end - f.start:
                    continue
                matches = entries.get(target, [])
                if not matches and opcode != 3:
                    # Tail branches can enter a shared epilogue inside a peer
                    # function. Resolve its unique containing body, not a label
                    # spelling or an assumed entry at that address.
                    at = bisect_right(addresses, target) - 1
                    matches = []
                    while at >= 0 and maximum_ends[at] > target:
                        candidate = by_address[at]
                        if target < candidate.address + candidate.end - candidate.start:
                            matches.append(candidate)
                        at -= 1
                if len(matches) != 1:
                    unresolved[key] = "symbol-graph-target-unresolved"
                    continue
                peer = (version, matches[0].start)
                outgoing[key].add(peer)
                incoming[peer].add(key)
        del image
    return outgoing, incoming, unresolved


def join_symbols(
    images: Mapping[str, bytes | Rom],
    inventories: dict[str, list[split.Function]],
    root: Callable[[Key], Key],
    join: Callable[[Key, Key], None],
    reasons: dict[Key, str],
    details: dict[Key, dict[str, Any]],
    loaded_spans: Mapping[str, list[Span]] | None = None,
) -> set[Key]:
    """Propose bounded positions, then prove the whole mapped call graph.

    Equal counts between consecutive matched anchors give exactly one ordered
    positional mapping. All version pairs are checked together. Candidate
    components with duplicate versions are refused; graph refusals propagate
    to dependent candidates until the remaining graph agrees without guesses.
    """
    ordered = {v: sorted(ff, key=lambda f: f.start) for v, ff in inventories.items()}
    members: dict[Key, list[Key]] = defaultdict(list)
    for v, ff in ordered.items():
        for f in ff:
            members[root((v, f.start))].append((v, f.start))
    proposals: dict[Key, set[Key]] = defaultdict(set)
    for a, b in combinations(inventories, 2):
        shared = {group for group, rows in members.items() if {a, b} <= {v for v, _ in rows}}
        anchors = {
            v: [(i, root((v, f.start))) for i, f in enumerate(ordered[v]) if root((v, f.start)) in shared]
            for v in (a, b)
        }
        pairs = {(left[1], right[1]): (left[0], right[0]) for left, right in pairwise(anchors[b])}
        source_pairs = {(left[1], right[1]) for left, right in pairwise(anchors[a])}
        for (lo, left), (hi, right) in pairwise(anchors[b]):
            if (left, right) not in source_pairs:
                for f in ordered[b][lo + 1 : hi]:
                    reasons[b, f.start] = "symbol-anchor-order"
        for (lo, left), (hi, right) in pairwise(anchors[a]):
            source = [(a, f.start) for f in ordered[a][lo + 1 : hi]]
            if (left, right) not in pairs:
                for key in source:
                    reasons[key] = "symbol-anchor-order"
                continue
            start, end = pairs[left, right]
            target = [(b, f.start) for f in ordered[b][start + 1 : end]]
            if len(source) != len(target):
                for key in source + target:
                    reasons[key] = "symbol-position-count-mismatch"
                continue
            for x, y in zip(source, target, strict=True):
                first, second = root(x), root(y)
                if first != second:
                    proposals[first].add(second)
                    proposals[second].add(first)
                    proof = {
                        "versions": [a, b],
                        "left_anchor": list(left),
                        "right_anchor": list(right),
                        "positions": [list(x), list(y)],
                    }
                    for key in (x, y):
                        details.setdefault(key, {}).setdefault("anchor_positions", []).append(proof)
    components: dict[Key, list[Key]] = {}
    seen: set[Key] = set()
    for seed in proposals:
        if seed in seen:
            continue
        component, pending = set(), [seed]
        while pending:
            key = pending.pop()
            if key in component:
                continue
            component.add(key)
            pending.extend(proposals[key] - component)
        seen.update(component)
        rows = [key for group in sorted(component) for key in members[group]]
        if len({v for v, _ in rows}) != len(rows):
            for key in rows:
                reasons[key] = "symbol-position-conflict"
        else:
            components[seed] = rows
    outgoing, incoming, unresolved = graph(images, inventories, details, loaded_spans)
    while components:
        tentative = {root(key): seed for seed, rows in components.items() for key in rows}

        def mapped(key: Key, tentative: dict[Key, Key] = tentative) -> Key:
            group = root(key)
            return tentative.get(group, group)

        refused = {}
        for seed, rows in components.items():
            unknown = next((unresolved[key] for key in rows if key in unresolved), None)
            if unknown:
                refused[seed] = unknown
                continue
            profiles = [
                (frozenset(mapped(k) for k in incoming[key]), frozenset(mapped(k) for k in outgoing[key]))
                for key in rows
            ]
            if any(profile != profiles[0] for profile in profiles[1:]):
                refused[seed] = "symbol-graph-mismatch"
            elif not any(profiles[0]):
                refused[seed] = "symbol-graph-no-evidence"
        if not refused:
            break
        for seed, reason in refused.items():
            for key in components.pop(seed):
                reasons[key] = reason
    joined = set()
    for rows in components.values():
        for key in rows:
            join(rows[0], key)
            joined.add(key)
    for key, detail in details.items():
        detail["reason"] = "anchor-call-graph" if key in joined else reasons[key]
        detail["callers"] = [list(root(k)) for k in sorted(incoming[key])]
        detail["callees"] = [list(root(k)) for k in sorted(outgoing[key])]
        detail["graph_rule"] = "all resolved caller and callee items agree; no unresolved transfers"
    return joined
