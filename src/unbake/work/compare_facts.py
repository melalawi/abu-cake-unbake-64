"""Chosen machine-word diagnostics. Raw scores and acceptance stay with score/Family.

Deltas are relative to the target: negative raw byte deltas count differing target
words, separately from insertions and size. Subtract two bound captures to inspect
trial gains; a regional gain never establishes whole-function acceptance.
"""

from __future__ import annotations

from bisect import bisect_right
from collections import Counter, defaultdict
from difflib import SequenceMatcher
from itertools import pairwise
from typing import TYPE_CHECKING, Any

from unbake.work.score import Compare, align_words, classify

if TYPE_CHECKING:
    from unbake.config import Project
    from unbake.work.compare import Compared


def _branch(word: int) -> bool:
    op = word >> 26
    return op in (1, 4, 5, 6, 7, 20, 21, 22, 23) or (op in (16, 17, 18) and (word >> 21) & 31 == 8)


def _destination(word: int, index: int, pc: int) -> int | None:
    if _branch(word):
        immediate = word & 65535
        return index + 1 + (immediate if immediate < 32768 else immediate - 65536)
    if word >> 26 in (2, 3):
        return ((((pc + index * 4 + 4) & 0xF0000000) | ((word & 0x3FFFFFF) << 2)) - pc) // 4
    return None


def _op(word: int) -> str:
    if word == 0:
        return "nop"
    if word >> 26 == 0:
        return {6: "srlv", 7: "srav", 8: "jr", 9: "jalr", 33: "addu"}.get(word & 63, "SPECIAL")
    return {2: "j", 3: "jal", 4: "beq", 5: "bne", 10: "slti"}.get(word >> 26, f"op{word >> 26}")


def _word(index: int, candidate: int | None, target: tuple[int, ...], other: tuple[int, ...]) -> dict[str, Any]:
    a, b = target[index], None if candidate is None else other[candidate]
    return {
        "target_offset": index * 4,
        "candidate_offset": None if candidate is None else candidate * 4,
        "target_word": f"{a:08X}",
        "candidate_word": None if b is None else f"{b:08X}",
        "target_op": _op(a),
        "candidate_op": None if b is None else _op(b),
        "target_registers": [(a >> shift) & 31 for shift in (21, 16, 11)],
        "candidate_registers": None if b is None else [(b >> shift) & 31 for shift in (21, 16, 11)],
    }


def facts(result: Compare, pc: int, names: dict[int, list[str]]) -> dict[str, Any]:
    target, candidate = result.target_words, result.candidate_words
    mapping: dict[int, int] = {}
    different: dict[int, int | None] = {}
    inserted: list[int] = []
    for tag, a, b, c, d in SequenceMatcher(None, target, candidate, autojunk=False).get_opcodes():
        if tag == "equal":
            mapping.update(zip(range(a, b), range(c, d), strict=True))
        else:
            pairs = min(b - a, d - c) if tag == "replace" else 0
            different.update((a + k, c + k) for k in range(pairs))
            different.update((i, None) for i in range(a + pairs, b))
            inserted.extend(range(c + pairs, d))
    if len(different) != result.of - result.identical:
        raise ValueError("chosen score and retained words disagree")
    masks = {i: 0xFFFF if _branch(w) else 0x3FFFFFF for i, w in enumerate(candidate) if _branch(w) or w >> 26 in (2, 3)}
    diagnostic: dict[int, int] = {}
    if masks and different:
        for tag, a, b, c, d in align_words(target, candidate, masks):
            if tag in ("equal", "replace") and b - a == d - c:
                diagnostic.update(zip(range(a, b), range(c, d), strict=True))
    # Index each anchor once, rather than scanning whole words for every displacement.
    anchors_t = Counter(target[i : i + 3] for i in range(len(target) - 2))
    anchors_c = Counter(candidate[i : i + 3] for i in range(len(candidate) - 2))
    rows = []
    for i, raw_j in different.items():
        candidate_index = diagnostic.get(i, raw_j)
        word_t, word_c = target[i], None if candidate_index is None else candidate[candidate_index]
        bucket, reason = "structural_candidate", "missing or changed instruction/register"
        if word_c is not None and classify(word_t, word_c) == "immediate":
            assert candidate_index is not None
            bucket, reason = "unresolved", "immediate/literal owner is not established"
            if (_branch(word_t) and word_t >> 16 == word_c >> 16) or word_t >> 26 == word_c >> 26 == 2:
                dest_t, dest_c = _destination(word_t, i, pc), _destination(word_c, candidate_index, pc)
                if dest_t is not None and dest_c is not None and mapping.get(dest_t) == dest_c:
                    key = target[dest_t : dest_t + 3]
                    if (
                        len(key) == 3
                        and key == candidate[dest_c : dest_c + 3]
                        and anchors_t[key] == anchors_c[key] == 1
                    ):
                        bucket, reason = "consequential_proven", "unique raw-equal three-word destination anchor"
            elif word_t >> 26 == word_c >> 26 == 3:
                bucket, reason = "structural_candidate", "different direct-call target"
        rows.append(_word(i, candidate_index, target, candidate) | {"bucket": bucket, "reason": reason})
    boundaries = {0, len(target)}
    edges = []
    for i, w in enumerate(target):
        if _branch(w) or w >> 26 == 2 or (w >> 26 == 0 and w & 63 in (8, 9)):
            boundaries.add(min(i + 2, len(target)))
            destination = _destination(w, i, pc)
            if destination is not None and 0 <= destination < len(target):
                boundaries.add(destination)
                edges.append({"offset": i * 4, "destination": destination * 4, "delay_slot": (i + 1) * 4})
    # Coalesce target blocks without splitting any transfer from its delay slot.
    bounds = sorted(boundaries)
    spans: list[tuple[int, int]] = []
    for a, b in pairwise(bounds):
        if spans and b - spans[-1][0] <= 128:
            spans[-1] = spans[-1][0], b
        else:
            spans.append((a, b))
    starts = [a for a, _ in spans]
    grouped: defaultdict[int, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[bisect_right(starts, row["target_offset"] // 4) - 1].append(row)
    inverse = {j: i for i, j in mapping.items()}
    inverse.update({j: i for i, j in different.items() if j is not None})
    keys = sorted(inverse)
    insertion_counts: Counter[int] = Counter()
    for j in inserted:
        owner = inverse[keys[max(0, bisect_right(keys, j) - 1)]] if keys else 0
        insertion_counts[bisect_right(starts, owner) - 1] += 1
    regions = []
    remaining = 64
    for index, (a, b) in enumerate(spans):
        rr = grouped[index]
        correspondence = [mapping.get(i, diagnostic.get(i)) for i in range(a, b)]
        mapped = [j for j in correspondence if j is not None]
        lo, hi = (min(mapped), max(mapped) + 1) if mapped else (0, 0)
        calls = []
        for side, words, left, right in (("target", target, a, b), ("candidate", candidate, lo, hi)):
            for i in range(left, right):
                w = words[i]
                if w >> 26 == 3:
                    destination = _destination(w, i, pc)
                    assert destination is not None
                    address = pc + destination * 4
                    calls.append(
                        {
                            "side": side,
                            "offset": i * 4,
                            "address": hex(address),
                            "aliases": sorted(names.get(address, [])),
                            "identity": "bound" if address in names else "unresolved",
                        }
                    )
                elif w >> 26 == 0 and w & 63 == 9:
                    calls.append({"side": side, "offset": i * 4, "identity": "indirect-unresolved"})
        context: list[dict[str, Any]] = []
        # Equal return/predicate context shares the same global row budget.
        if rr and remaining:
            center = rr[0]["target_offset"] // 4
            for i in range(max(0, center - 4), min(len(target), center + 3)):
                if i in mapping and len(context) < min(6, remaining):
                    context.append(_word(i, mapping[i], target, candidate))
        remaining -= len(context)
        displayed = rr[:remaining]
        remaining -= len(displayed)
        regions.append(
            {
                "id": f"{a * 4:06x}-{b * 4:06x}",
                "target_span": [a * 4, b * 4],
                "candidate_span": [lo * 4, hi * 4] if mapped else None,
                "different": len(rr),
                "identical": b - a - len(rr),
                "raw_target_delta_bytes": -4 * len(rr),
                "inserted_alignment_rows": insertion_counts[index],
                "buckets": dict(Counter(r["bucket"] for r in rr)),
                "calls": calls,
                "rows": displayed,
                "equal_context": context,
                "rows_total": len(rr),
                "truncated": len(displayed) < len(rr),
            }
        )
    return {
        "pc": hex(pc),
        "target_different": len(rows),
        "raw_target_delta_bytes": -4 * len(rows),
        "size_delta_bytes": 4 * (len(candidate) - len(target)),
        "buckets": dict(Counter(r["bucket"] for r in rows)),
        "raw_inserted_rows": len(inserted),
        "scored_inserted": result.typed["inserted"],
        "scored_order": result.typed["order"],
        "regions": regions,
        "all_word_rows": rows,
        "target_internal_edges": edges,
        "work_counts": {
            "native": 0,
            "raw_correspondence": 1,
            "diagnostic_correspondence": int(bool(masks and different)),
            "per_region_alignments": 0,
        },
    }


def attach(project: Project, chosen: Compared) -> None:
    """Resolve each holding version's symbols only after Family chooses the recipe."""
    from unbake.layout import split
    from unbake.work.compare import row_of

    for version, result in chosen.compares.items():
        if not result.target_words or version in chosen.faults:
            continue
        row = row_of(project, chosen.function, version)
        _, symbols = split.symbols(project.version(version).symbols)
        names: defaultdict[int, list[str]] = defaultdict(list)
        for name, (address, _, _) in symbols.items():
            names[address].append(name)
        chosen.facts[version] = facts(result, row.address, dict(names)) | {
            "function": chosen.function,
            "version": version,
            "source_sha256": chosen.source_sha256,
            "compiler": chosen.compiler,
        }


def lines(version: str, data: dict[str, Any]) -> list[str]:
    output = [
        f"VERSION {version} facts: target-different={data['target_different']}; {data['buckets']}; "
        f"raw target delta={data['raw_target_delta_bytes']} B (local deltas do not certify acceptance)"
    ]
    for region in data["regions"]:
        for row in [*region["rows"], *region["equal_context"]]:
            output.append(
                f"+{row['target_offset']:04X}: {row['target_op']} {row['target_word']} / "
                f"{row['candidate_op']} {row['candidate_word']} {row.get('bucket', 'raw-equal neighborhood')}"
            )
    return output
