"""Allocator evidence and explanations of aligned register differences."""

from __future__ import annotations

import tempfile
from dataclasses import replace
from pathlib import Path
from typing import cast

from unbake.compilers.families import family_named
from unbake.compilers.families.types import Allocation, Pseudo, RegisterDifference, Schedule
from unbake.config import Held, Host, Project
from unbake.process import named as cause_named
from unbake.work.score import Measurement


def align(allocation: Allocation, comparison: Measurement) -> Allocation:
    """Attach the trial's aligned register words to possible pseudos and winners."""
    if allocation.linked_sha256 != comparison.strict.get("linked_sha256"):
        return replace(
            allocation,
            differences=(),
            instruction_origins=(),
            limitations=(*allocation.limitations, "diagnostic recipe has no equal linked-byte proof"),
        )
    differences = []
    for target_offset, draft_offset, before, after in comparison.register_changes:
        origins = [(uid, numbers) for offset, uid, numbers in allocation.instruction_origins if offset == draft_offset]
        candidates = tuple(
            p.number
            for p in allocation.pseudos
            if p.hard == after
            and any(
                p.number in numbers and (p.live_range is None or p.live_range[0] <= uid <= p.live_range[1])
                for uid, numbers in origins
            )
        )
        holders = ()  # target RTL is unavailable; hard-register names are not pseudo identity
        differences.append(
            RegisterDifference(
                target_offset,
                draft_offset,
                before,
                after,
                candidates,
                holders,
                len(candidates) != 1 or len(holders) != 1,
            )
        )
    return replace(allocation, differences=tuple(differences))


def leverage(allocation: Allocation) -> tuple[Pseudo, ...]:
    """Rank source-addressable pseudos by affected words, then priority pressure."""
    counts = {p.number: sum(p.number in d.candidates for d in allocation.differences) for p in allocation.pseudos}
    return tuple(
        sorted(
            (p for p in allocation.pseudos if p.name is not None),
            key=lambda p: (-counts[p.number], -(p.priority if p.priority is not None else 0), p.number),
        )
    )


def render(allocation: Allocation) -> str:
    rows = list(allocation.limitations)
    if allocation.pseudos and all(p.allocator == "local" and p.rank is None for p in allocation.pseudos):
        rows.append("Global allocation order is empty; no pseudos to rank.")
    by_number = {p.number: p for p in allocation.pseudos}
    for pseudo in sorted((p for p in allocation.pseudos if p.rank is not None), key=lambda p: cast(int, p.rank)):
        rows.append(
            f"allocation rank {pseudo.rank}: pseudo {pseudo.number} in {pseudo.hard}; "
            f"priority={pseudo.priority} refs={pseudo.references} length={pseudo.live_length}"
        )
    for difference in allocation.differences:
        rows.append(
            f"+0x{difference.target_offset:04X}: register {difference.draft_hard} wants "
            f"{difference.target_hard}; ambiguous={difference.ambiguous}"
        )
        if allocation.family:
            rows.extend(family_named(allocation.family).allocation_hints(difference, by_number))
        for role, numbers in (("candidate", difference.candidates), ("holder", difference.holders)):
            for number in numbers[:5]:
                p = by_number[number]
                rows.append(
                    f"  {role} pseudo {number}: priority={p.priority} refs={p.references} "
                    f"live={p.live_range} length={p.live_length} allocator={p.allocator}"
                )
                if len(numbers) > 5:
                    rows.append(f"    {len(numbers) - 5} further relevant pseudos retained")
                rows.extend(
                    f"    rejected {hard}: {why}" for hard, why in p.rejections if hard == difference.target_hard
                )
    if not allocation.differences:
        rows.append("No aligned register differences; no register leverage found.")
    return "\n".join(rows)


def allocation(project: Project, policy: Host, source: Path, version: str) -> Allocation:
    """Measure one trial and request diagnostic evidence from its family."""
    from unbake.compilers import registry as toolchain
    from unbake.compilers.families import family_for
    from unbake.work import compare

    source = Path(source).resolve()
    if not source.is_file():
        raise Held(
            cause_named(f"source.{source}", f"source.{source}: missing file", owner="decomp.explain", stage="explain")
        )
    project.version(version)
    compiler = project.compiler_for(source)
    family = family_for(compiler)
    toolchain.verify(
        toolchain.compiler_directory(project.tools, toolchain.specification(compiler.id)),
        toolchain.specification(compiler.id),
    )
    root = project.work / "_explain"
    root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=source.stem + ".", dir=root) as temporary:
        comparison = compare.measure(project, policy, source, versions=(version,)).compares[version]
        result = family.collect_allocation(project, policy, source, version, Path(temporary))
        return align(result, comparison)


def _absolute_includes(project: Project, flags: list[str] | tuple[str, ...]) -> list[str]:
    values = list(flags)
    for index, flag in enumerate(values):
        if flag in ("-I", "-include", "-isystem", "-imacros", "-iquote"):
            if index + 1 >= len(values):
                raise Held(
                    cause_named(
                        f"compiler.cflags.{flag}",
                        f"compiler.cflags.{flag}: missing value",
                        owner="decomp.explain",
                        stage="explain",
                    )
                )
            path = Path(values[index + 1])
            if not path.is_absolute():
                values[index + 1] = str(project.root / path)
        elif flag.startswith("-I") and len(flag) > 2 and not Path(flag[2:]).is_absolute():
            values[index] = "-I" + str(project.root / flag[2:])
    return values


def order(project: Project, policy: Host, source: Path, version: str) -> Schedule:
    """Request scheduling evidence only when the family can produce it."""
    from unbake.compilers import registry as toolchain
    from unbake.compilers.families import family_for

    source = Path(source).resolve()
    if not source.is_file():
        raise Held(
            cause_named(f"source.{source}", f"source.{source}: missing file", owner="decomp.explain", stage="explain")
        )
    project.version(version)
    compiler = project.compiler_for(source)
    family = family_for(compiler)
    if not family.schedule_available():
        return family.schedule(None)
    toolchain.verify(
        toolchain.compiler_directory(project.tools, toolchain.specification(compiler.id)),
        toolchain.specification(compiler.id),
    )
    root = project.work / "_explain"
    root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=source.stem + ".", dir=root) as temporary:
        return family.collect_schedule(project, policy, source, version, Path(temporary))
