"""Symbol correspondence independent of executable body equality."""

from __future__ import annotations

from bisect import bisect_right
from collections import defaultdict
from collections.abc import Callable, Mapping
from itertools import combinations, pairwise
from typing import Any

from unbake.layout import split
from unbake.layout.rodata_references import collect, words
from unbake.project.config import SymbolPolicy
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


class Bodies:
    """Symmetric masked-instruction similarity; arithmetic immediates stay fixed."""

    def __init__(self, images: Mapping[str, bytes | Rom], inventories: dict[str, list[split.Function]]) -> None:
        from unbake.layout.xver import _masks

        self.code: dict[Key, list[tuple[int, int]]] = {}
        self.cache: dict[tuple[Key, Key], float] = {}
        for version in sorted(inventories):
            cartridge = images[version]
            image = cartridge if isinstance(cartridge, bytes) else cartridge.image()
            for f in inventories[version]:
                code = words(image[f.start : f.end])
                while len(code) > 2 and code[-1] == 0 and code[-2] != 0x03E00008:
                    code.pop()
                self.code[version, f.start] = [
                    (word & ~mask, mask) for word, mask in zip(code, _masks(code), strict=True)
                ]
            del image

    def score(self, x: Key, y: Key) -> float:
        from difflib import SequenceMatcher

        first, second = sorted((x, y))
        pair = (first, second)
        if pair not in self.cache:
            a, b = (self.code[key] for key in pair)
            self.cache[pair] = min(
                SequenceMatcher(None, a, b, autojunk=False).ratio(),
                SequenceMatcher(None, b, a, autojunk=False).ratio(),
            )
        return self.cache[pair]

    def proof(self, x: Key, y: Key, alternative: float, policy: SymbolPolicy) -> dict[str, Any]:
        from difflib import SequenceMatcher

        a, b = self.code[x], self.code[y]
        changes = [
            {"operation": op, "source_words": [lo, hi], "target_words": [start, end]}
            for op, lo, hi, start, end in SequenceMatcher(None, a, b, autojunk=False).get_opcodes()
            if op != "equal"
        ]
        return {
            "metric": "minimum directional SequenceMatcher ratio over (masked instruction, relocation mask)",
            "score": self.score(x, y),
            "best_alternative": alternative,
            "threshold": policy.similarity_threshold,
            "required_margin": policy.similarity_margin,
            "margin": self.score(x, y) - alternative,
            "instruction_counts": [len(a), len(b)],
            "instruction_diff": changes,
        }


def alignment(scores: list[list[float]], eligible: set[tuple[int, int]]) -> set[tuple[int, int]]:
    """Return only diagonal edges present in every maximum-weight edit alignment.

    Skip edges have zero weight. Counting optimal paths avoids choosing a
    convenient tie: an edge is forced only when every optimal path uses it.
    Integer millionths make score comparisons reproducible.
    """
    n, m = len(scores), len(scores[0]) if scores else 0
    weights = {(i, j): round(scores[i][j] * 1_000_000) for i, j in eligible}

    def table(reverse: bool) -> tuple[list[list[int]], list[list[int]]]:
        values = [[0] * (m + 1) for _ in range(n + 1)]
        paths = [[0] * (m + 1) for _ in range(n + 1)]
        paths[0][0] = 1
        for i in range(n + 1):
            for j in range(m + 1):
                if i == j == 0:
                    continue
                choices = []
                if i:
                    choices.append((values[i - 1][j], paths[i - 1][j]))
                if j:
                    choices.append((values[i][j - 1], paths[i][j - 1]))
                edge = (n - i, m - j) if reverse else (i - 1, j - 1)
                if i and j and edge in weights:
                    choices.append((values[i - 1][j - 1] + weights[edge], paths[i - 1][j - 1]))
                best = max(value for value, _ in choices)
                values[i][j] = best
                paths[i][j] = sum(count for value, count in choices if value == best)
        return values, paths

    forward, prefix = table(False)
    backward, suffix = table(True)
    optimum, total = forward[n][m], prefix[n][m]
    return {
        (i, j)
        for (i, j), weight in weights.items()
        if forward[i][j] + weight + backward[n - i - 1][m - j - 1] == optimum
        and prefix[i][j] * suffix[n - i - 1][m - j - 1] == total
    }


def join_symbols(
    images: Mapping[str, bytes | Rom],
    inventories: dict[str, list[split.Function]],
    root: Callable[[Key], Key],
    join: Callable[[Key, Key], None],
    reasons: dict[Key, str],
    details: dict[Key, dict[str, Any]],
    policy: SymbolPolicy,
    loaded_spans: Mapping[str, list[Span]] | None = None,
) -> set[Key]:
    """Propose, check and join in simultaneous rounds until identity stabilizes.

    Ordered anchors bound proposals. Unequal counts use forced edit-alignment
    pairs; graph-free leaves require high, mutually distinctive similarity.
    Conflicts and graph refusals propagate through the tentative components.
    Every successful round supplies anchors for the next round.
    """
    ordered = {v: sorted(inventories[v], key=lambda f: f.start) for v in sorted(inventories)}
    outgoing, incoming, unresolved = graph(images, inventories, details, loaded_spans)
    bodies = Bodies(images, inventories)
    joined: set[Key] = set()
    iteration = -1
    while True:
        iteration += 1
        members: dict[Key, list[Key]] = defaultdict(list)
        for v, ff in ordered.items():
            for f in ff:
                members[root((v, f.start))].append((v, f.start))
        proposals: dict[Key, set[Key]] = defaultdict(set)
        proofs: dict[tuple[Key, Key], dict[str, Any]] = {}
        for a, b in combinations(ordered, 2):
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
                # Refuse pathological intervals before allocating quadratic tables.
                if len(source) * len(target) > 250_000:
                    for key in source + target:
                        reasons[key] = "symbol-alignment-budget"
                    continue
                scores = [[bodies.score(x, y) for y in target] for x in source]
                alternatives = {
                    (i, j): max(
                        [scores[i][k] for k in range(len(target)) if k != j]
                        + [scores[k][j] for k in range(len(source)) if k != i]
                        + [0.0]
                    )
                    for i in range(len(source))
                    for j in range(len(target))
                }
                eligible = {
                    (i, j)
                    for (i, j), alternative in alternatives.items()
                    if scores[i][j] >= policy.similarity_threshold
                    and scores[i][j] - alternative + 1e-12 >= policy.similarity_margin
                }
                unequal = len(source) != len(target)
                # Establish the stronger existing position/graph evidence first.
                # Later alignment proposals cannot invalidate an already proved join.
                if iteration == 0 and unequal:
                    for key in source + target:
                        reasons[key] = "symbol-position-count-mismatch"
                    continue
                selected = alignment(scores, eligible) if unequal else {(i, i) for i in range(len(source))}
                matched = {source[i] for i, _ in selected} | {target[j] for _, j in selected}
                if unequal:
                    for key in source + target:
                        if key not in matched:
                            reasons[key] = (
                                "symbol-alignment-ambiguous"
                                if any(
                                    scores[i][j] >= policy.similarity_threshold
                                    for i in range(len(source))
                                    for j in range(len(target))
                                    if key in (source[i], target[j])
                                )
                                else "symbol-alignment-insertion-deletion"
                            )
                            details.setdefault(key, {})["alignment"] = {
                                "round": iteration,
                                "counts": [len(source), len(target)],
                                "left_anchor": list(left),
                                "right_anchor": list(right),
                                "reason": reasons[key],
                            }
                for i, j in sorted(selected):
                    x, y = source[i], target[j]
                    first, second = root(x), root(y)
                    if first == second:
                        continue
                    proof = {
                        "round": iteration,
                        "versions": [a, b],
                        "left_anchor": list(left),
                        "right_anchor": list(right),
                        "positions": [list(x), list(y)],
                        "counts": [len(source), len(target)],
                        "position_rule": "forced-sequence-alignment" if unequal else "equal-count-position",
                        "similarity": bodies.proof(x, y, alternatives[i, j], policy),
                        "similarity_admissible": (i, j) in eligible,
                        "graph_free": not (incoming[x] or outgoing[x] or incoming[y] or outgoing[y]),
                    }
                    proofs[x, y] = proof
                    proposals[first].add(second)
                    proposals[second].add(first)
                    for key in (x, y):
                        details.setdefault(key, {}).setdefault("anchor_positions", []).append(proof)
        components: dict[Key, list[Key]] = {}
        seen: set[Key] = set()
        for seed in sorted(proposals):
            if seed in seen:
                continue
            component, pending = set(), [seed]
            while pending:
                key = pending.pop()
                if key in component:
                    continue
                component.add(key)
                pending.extend(sorted(proposals[key] - component))
            seen.update(component)
            rows = sorted(key for group in sorted(component) for key in members[group])
            if len({v for v, _ in rows}) != len(rows):
                for key in rows:
                    reasons[key] = "symbol-position-conflict"
            else:
                components[seed] = rows
        rules = {}
        while components:
            tentative = {root(key): seed for seed, rows in components.items() for key in rows}

            def mapped(key: Key, tentative: dict[Key, Key] = tentative) -> Key:
                group = root(key)
                return tentative.get(group, group)

            mapped_versions: dict[Key, set[str]] = defaultdict(set)
            for group, keys in members.items():
                mapped_versions[mapped(group)].update(v for v, _ in keys)
            refused = {}
            for seed, rows in sorted(components.items()):
                unknown = next((unresolved[key] for key in rows if key in unresolved), None)
                if unknown:
                    refused[seed] = unknown
                    continue
                profiles = [
                    (frozenset(mapped(k) for k in incoming[key]), frozenset(mapped(k) for k in outgoing[key]))
                    for key in rows
                ]
                caller_checks = []
                caller_mismatch = False
                caller_evidence = True
                for i, j in combinations(range(len(rows)), 2):
                    versions = {rows[i][0], rows[j][0]}
                    comparable = {
                        group for group in profiles[i][0] | profiles[j][0] if versions <= mapped_versions[group]
                    }
                    first_callers, second_callers = profiles[i][0] & comparable, profiles[j][0] & comparable
                    caller_checks.append(
                        {
                            "positions": [list(rows[i]), list(rows[j])],
                            "callers": [
                                [list(k) for k in sorted(first_callers)],
                                [list(k) for k in sorted(second_callers)],
                            ],
                        }
                    )
                    caller_mismatch |= first_callers != second_callers
                    caller_evidence &= bool(first_callers)
                graph_evidence = bool(profiles[0][1]) or caller_evidence
                relevant = [proof for pair, proof in proofs.items() if set(pair) <= set(rows)]
                if caller_mismatch or any(profile[1] != profiles[0][1] for profile in profiles[1:]):
                    refused[seed] = "symbol-graph-mismatch"
                    refusal = {
                        "round": iteration,
                        "positions": [list(key) for key in rows],
                        "comparable_callers": caller_checks,
                        "profiles": [
                            {
                                "callers": [list(k) for k in sorted(profile[0])],
                                "callees": [list(k) for k in sorted(profile[1])],
                            }
                            for profile in profiles
                        ],
                    }
                    for key in rows:
                        details.setdefault(key, {})["graph_refusal"] = refusal
                elif not graph_evidence:
                    if iteration == 0:
                        refused[seed] = "symbol-graph-no-evidence"
                        continue
                    # Check transitive members too: a chain of near matches must
                    # not join two dissimilar endpoints through a third version.
                    if any(bodies.score(x, y) < policy.similarity_threshold for x, y in combinations(rows, 2)):
                        refused[seed] = "symbol-leaf-similarity-low"
                    elif not all(proof["similarity_admissible"] for proof in relevant):
                        refused[seed] = "symbol-leaf-similarity-ambiguous"
                    else:
                        rules[seed] = "anchor-leaf-similarity"
                else:
                    rules[seed] = (
                        "anchor-sequence-alignment"
                        if any(proof["position_rule"] == "forced-sequence-alignment" for proof in relevant)
                        else "anchor-call-graph"
                    )
            if not refused:
                break
            for seed, reason in sorted(refused.items()):
                for key in components.pop(seed):
                    reasons[key] = reason
        if not components:
            if iteration == 0:
                continue
            break
        graph_proofs = {
            seed: [
                {
                    "position": list(key),
                    "callers": [list(mapped(k)) for k in sorted(incoming[key])],
                    "callees": [list(mapped(k)) for k in sorted(outgoing[key])],
                }
                for key in rows
            ]
            for seed, rows in sorted(components.items())
        }
        for seed, rows in sorted(components.items()):
            accepted = [proof for pair, proof in sorted(proofs.items()) if set(pair) <= set(rows)]
            for key in rows:
                join(rows[0], key)
                joined.add(key)
                detail = details.setdefault(key, {})
                detail["reason"] = rules[seed]
                detail.setdefault("joins", []).append(
                    {"round": iteration, "rule": rules[seed], "graph": graph_proofs[seed], "evidence": accepted}
                )
    for key, detail in details.items():
        if key not in joined:
            detail["reason"] = reasons[key]
        detail["fixpoint_rounds"] = iteration - 1
        detail["callers"] = [list(root(k)) for k in sorted(incoming[key])]
        detail["callees"] = [list(root(k)) for k in sorted(outgoing[key])]
        detail["graph_rule"] = (
            "all resolved callees agree; caller edges agree between versions containing that caller; "
            "no unresolved transfers"
        )
    return joined


def similarity_distribution(details: dict[str, dict[int, dict[str, Any]]]) -> dict[str, Any]:
    """Count each position comparison once per round, including refusals."""
    from collections import Counter

    comparisons = {}
    accepted = set()
    leaves = set()
    for rows in details.values():
        for detail in rows.values():
            for proof in detail.get("anchor_positions", []):
                key = (proof["round"], tuple(tuple(p) for p in proof["positions"]))
                comparisons[key] = proof["similarity"]["score"]
                if proof["graph_free"]:
                    leaves.add(key)
            for join in detail.get("joins", []):
                for proof in join["evidence"]:
                    accepted.add((proof["round"], tuple(tuple(p) for p in proof["positions"])))

    def bucket(score: float) -> str:
        return (
            "1.0"
            if score == 1
            else "0.9..<1"
            if score >= 0.9
            else "0.8..<0.9"
            if score >= 0.8
            else "0.5..<0.8"
            if score >= 0.5
            else "0..<0.5"
        )

    return {
        "unit": "unique proposed position pair per round",
        "comparisons": len(comparisons),
        "accepted": len(accepted),
        "leaf_comparisons": len(leaves),
        "leaf_proposed_bins": dict(sorted(Counter(bucket(comparisons[key]) for key in leaves).items())),
        "leaf_accepted_bins": dict(sorted(Counter(bucket(comparisons[key]) for key in leaves & accepted).items())),
        "proposed_bins": dict(sorted(Counter(bucket(score) for score in comparisons.values()).items())),
        "accepted_bins": dict(
            sorted(Counter(bucket(score) for key, score in comparisons.items() if key in accepted).items())
        ),
    }
