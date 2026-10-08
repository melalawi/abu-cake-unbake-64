"""Retained defining/use slices across GCC passes; no native observation jobs."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from typing import Any


def lineage(dumps: Mapping[str, str], instructions: dict[int, dict[str, Any]]) -> dict[str, Any]:
    from unbake.compilers.families.gcc.dump_facts import records

    order = ("rtl", "lreg", "combine", "greg", "jump", "jump2", "sched", "sched2", "dbr")
    stages = {stage: records(dumps[stage]) for stage in order if stage in dumps}
    by_uid: dict[int, list[tuple[str, dict[str, Any]]]] = {}
    for stage, rows in stages.items():
        for row in rows:
            by_uid.setdefault(row["uid"], []).append((stage, row))
    slices = []
    for uid, mapped in sorted(instructions.items()):
        if not mapped.get("candidate_offsets"):
            continue
        history = by_uid.get(uid, [])
        early = history[0][1] if history else mapped
        relevant = {n for n in early["registers"] if n >= 64}
        definitions = []
        # The nearest prior definitions of used pseudos bound the causal slice.
        for stage, rows in stages.items():
            position = next((i for i, row in enumerate(rows) if row["uid"] == uid), None)
            before = []
            if position is not None:
                for row in reversed(rows[:position]):
                    # Block entry, jump or call kills a straight-line dominance
                    # claim. Numeric UIDs do not establish execution order.
                    if row.get("kind") in ("jump_insn", "call_insn", "code_label", "barrier"):
                        break
                    if row["destination"] in relevant:
                        before.append(row)
                before.reverse()
            nearest = {}
            for row in before:
                nearest[row["destination"]] = row
            for row in nearest.values():
                definitions.append(
                    {
                        "uid": row["uid"],
                        "pseudo": row["destination"],
                        "stage": stage,
                        "source_line": row["source_line"],
                        "rtl": row["rtl"],
                    }
                )
        unique = {(row["uid"], row["pseudo"], row["stage"]): row for row in definitions}
        retained = sorted(unique.values(), key=lambda row: (row["uid"], row["pseudo"], row["stage"]))
        slices.append(
            {
                "uid": uid,
                "candidate_offsets": mapped["candidate_offsets"],
                "pseudos": sorted(relevant),
                "source_text": mapped.get("source_text"),
                "stages": [stage for stage, _ in history],
                "definitions": retained[:5],
                "definitions_total": len(retained),
                "unavailable_target_rtl": True,
            }
        )
    return {
        "available": bool(slices),
        "slices": slices,
        "artifacts": {stage: hashlib.sha256(text.encode()).hexdigest() for stage, text in sorted(dumps.items())},
        "limit": 5,
        "missing": [stage for stage in order if stage not in stages],
    }
