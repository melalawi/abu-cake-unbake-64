"""Explicit compiler evidence, attached to the existing chosen comparison facts."""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path
from typing import Any

from unbake.config import Held, Host, Project
from unbake.work.compare import Compared
from unbake.work.score import Measurement, classify, words

UNAVAILABLE = "unavailable"
ROW_LIMIT = 64


def eligible(data: dict[str, Any], result: Measurement) -> list[dict[str, Any]]:
    """Register changes and observed moved raw words select regions, not generic buckets."""
    offsets = {t for t, _, _, _ in result.register_changes}
    offsets.update(
        row["target_offset"]
        for row in data["all_word_rows"]
        if row["candidate_word"] is not None
        and classify(int(row["target_word"], 16), int(row["candidate_word"], 16)) == "register"
    )
    if (result.typed or {}).get("order"):
        positions: defaultdict[int, list[int]] = defaultdict(list)
        for i, word in enumerate(result.candidate):
            positions[word].append(i * 4)
        for row in data["all_word_rows"]:
            if positions[int(row["target_word"], 16)]:
                offsets.add(row["target_offset"])
    return [r for r in data["regions"] if any(r["target_span"][0] <= t < r["target_span"][1] for t in offsets)]


def unavailable(regions: list[dict[str, Any]], family: str, reason: str) -> None:
    for region in regions:
        region["compiler_facts"] = {
            "available": False,
            "family": family,
            "reason": reason,
            "registers": UNAVAILABLE,
            "schedule": UNAVAILABLE,
            "search_options": [],
        }


def attach_decisions(data: dict[str, Any], result: Measurement, parsed: dict[str, Any]) -> None:
    """Bound presentation; retain ambiguity rather than selecting a plausible pseudo."""
    regions = eligible(data, result)
    if not parsed["available"]:
        unavailable(regions, parsed["family"], parsed.get("reason", "; ".join(parsed.get("limitations", []))))
        return
    instructions = parsed["instructions"]
    from unbake.compilers.families import family_named

    family = family_named(parsed["family"])
    changes: list[tuple[int, int, int, int]] = []
    for row in data["all_word_rows"]:
        if row["candidate_word"] is None:
            continue
        target_word, candidate_word = int(row["target_word"], 16), int(row["candidate_word"], 16)
        if classify(target_word, candidate_word) == "register":
            changes.extend(
                (row["target_offset"], row["candidate_offset"], t, c)
                for t, c in family.hard_register_changes(target_word, candidate_word)
            )
    by_offset: defaultdict[int, list[dict[str, Any]]] = defaultdict(list)
    for insn in instructions.values():
        for offset in insn["candidate_offsets"]:
            by_offset[offset].append(insn)
    by_pseudo = {p["pseudo"]: p for p in parsed["pseudos"]}
    remaining = ROW_LIMIT
    for region in regions:
        lo, hi = region["target_span"]
        registers = []
        for t, c, target_hard, candidate_hard in changes:
            if not lo <= t < hi:
                continue
            mapped = by_offset[c]
            numbers = sorted(
                {
                    n
                    for insn in mapped
                    for n in insn["registers"]
                    if n in by_pseudo
                    and isinstance(by_pseudo[n]["candidate_hard"], list)
                    and candidate_hard in by_pseudo[n]["candidate_hard"]
                    and (
                        by_pseudo[n].get("live_range") is None
                        or by_pseudo[n]["live_range"][0] <= insn["uid"] <= by_pseudo[n]["live_range"][1]
                    )
                }
            )
            unique = len(mapped) == len(numbers) == 1
            registers.append(
                {
                    "target_offset": t,
                    "candidate_offset": c,
                    "target_hard": target_hard,
                    "candidate_hard": candidate_hard,
                    "target_pseudo": UNAVAILABLE,
                    "mapping": "unique" if unique else "unavailable: ambiguous or missing RTL mapping",
                    "pseudos": [by_pseudo[n] for n in numbers[:5]] or UNAVAILABLE,
                    "pseudos_total": len(numbers),
                    "pseudos_truncated": len(numbers) > 5,
                }
            )
        registers.sort(key=lambda r: (r["target_offset"], r["candidate_offset"], r["target_hard"]))
        cspan = region.get("candidate_span")
        relevant_offsets = {c for t, c, _, _ in changes if lo <= t < hi}
        for row in region["rows"]:
            if row["candidate_offset"] is not None:
                relevant_offsets.add(row["candidate_offset"])
        if cspan:
            relevant_offsets.update(range(cspan[0], cspan[1], 4))
        schedule = [
            r for r in parsed["schedule"] if any(set(i["candidate_offsets"]) & relevant_offsets for i in r["ready"])
        ]
        register_count = len(registers)
        registers = registers[:remaining]
        remaining -= len(registers)
        bounded_schedule = []
        for row in schedule:
            if len(row["ready"]) > remaining:
                break
            bounded_schedule.append(row)
            remaining -= len(row["ready"])
        options = []
        for row in bounded_schedule:
            if row["pass"] != "sched" or len(row["ready"]) != 2:
                continue
            births = [r for r in row["ready"] if r["promotion"] == "birth"]
            updates = [
                r for r in row["ready"] if r["promotion"] == "none" and instructions.get(r["uid"], {}).get("in_place")
            ]
            if len(births) == len(updates) == 1 and all(len(r["candidate_offsets"]) == 1 for r in row["ready"]):
                birth, update = births[0], updates[0]
                if instructions[update["uid"]]["mode"] == instructions[birth["uid"]]["mode"] == "SI":
                    options.append(
                        {
                            "method": "scheduler-birth",
                            "birth_uid": birth["uid"],
                            "update_uid": update["uid"],
                            "birth_expression": birth["source_text"],
                            "update_expression": update["source_text"],
                            "update_line": update["source_line"],
                            "acceptance": "whole-function byte comparison in every holding version",
                        }
                    )
        search_reason = "unavailable: multiple competing mapped pairs" if len(options) > 1 else UNAVAILABLE
        options = options if len(options) == 1 and remaining else []
        remaining -= len(options)
        region["compiler_facts"] = {
            "available": True,
            "family": parsed["family"],
            "reason": UNAVAILABLE,
            "registers": registers or UNAVAILABLE,
            "schedule": bounded_schedule or UNAVAILABLE,
            "search_options": options,
            "search_reason": search_reason,
            "row_limit": ROW_LIMIT,
            "truncated": register_count > len(registers) or len(schedule) > len(bounded_schedule),
            "limitations": parsed["limitations"],
            "lineage": parsed.get("lineage", {"available": False}),
        }


def collect(project: Project, host: Host, chosen: Compared) -> None:
    """One uncached native dump recipe for each affected, chosen holding version."""
    from unbake import atomic, runner, scratch
    from unbake.compilers import choice, drivers
    from unbake.compilers.families import family_for
    from unbake.fold import provider_reuse
    from unbake.process import run_tool
    from unbake.typemap import namespace
    from unbake.work.compare import row_of, view_for

    selected = choice.selected(project, chosen.function, chosen.compiler)
    selected = view_for(selected, chosen.file, chosen.function)
    family = family_for(selected.compiler_for(chosen.function))
    flags = family.compare_dump_flags()
    from unbake.compilers.registry import specification

    family_name = specification(selected.compiler_for(chosen.function).id).family
    for version, result in chosen.compares.items():
        data = chosen.facts.get(version)
        if data is None:
            continue
        counts = data["work_counts"]
        counts.update(
            {
                key: 0
                for key in (
                    "dump_compiles",
                    "dump_preprocesses",
                    "dump_assembles",
                    "dump_files_read",
                    "dump_bytes_read",
                    "dump_parses",
                )
            }
        )
        regions = eligible(data, result)
        if not regions:
            continue
        if not flags:
            attach_decisions(data, result, family.compiler_facts({}, result.candidate, ""))
            continue
        try:
            with (
                provider_reuse.view(selected, host, (version,)) as view,
                namespace.comparison_view(view, host, chosen.file, chosen.file.read_text()) as (view, source),
                scratch.temporary(host, view, "compare", prefix="compare-dumps-") as temporary,
            ):
                work = Path(temporary)
                # Same NON_MATCHING policy as measure(), including a fuzzy src unit.
                from unbake.work import attempts

                non_matching = (
                    chosen.file.resolve() == (project.src / f"{chosen.function}.c").resolve()
                    and attempts.ledger(project).fuzzy(chosen.function) is not None
                )
                commands = drivers.steps(
                    view, version, chosen.function, str(source), runner.tools(host), non_matching=non_matching
                )
                counts["dump_preprocesses"] += 1
                expanded = drivers.run_preprocess(view, list(commands.preprocess), "compare", unit=chosen.function)
                atomic.text(work / f"{chosen.function}.i", expanded)
                counts["dump_compiles"] += 1
                run_tool([str(view.compiler_for(chosen.function).cc), *flags, *commands.compile[1:]], work, "compare")
                if commands.assemble:
                    counts["dump_assembles"] += 1
                    run_tool(list(commands.assemble), work, "compare")
                linked, _problems = runner.link_function(
                    project,
                    host,
                    work / f"{chosen.function}.o",
                    version,
                    row_of(project, chosen.function, version),
                    chosen.file,
                )
                if (
                    __import__("hashlib").sha256(linked).hexdigest() != result.strict.get("linked_sha256")
                    or words(linked) != result.candidate
                ):
                    unavailable(regions, family_name, "unavailable: diagnostic recipe changed candidate bytes")
                    continue
                dumps = {}
                for suffix in family.compare_dump_suffixes():
                    paths = sorted(work.glob("*." + suffix))
                    if len(paths) == 1:
                        content = paths[0].read_bytes()
                        counts["dump_files_read"] += 1
                        counts["dump_bytes_read"] += len(content)
                        dumps[suffix] = content.decode()
                counts["dump_parses"] += 1
                attach_decisions(
                    data, result, family.compiler_facts(dumps, result.candidate, expanded, chosen.function)
                )
        except (Held, OSError, UnicodeError) as error:
            unavailable(regions, family_name, f"unavailable: {error}")
