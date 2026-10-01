"""Derive and resolve compiler constants using explicit relocation evidence."""

import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from unbake.decomp.needs import Need, RodataNeed, register_deriver, register_resolver
from unbake.layout import split
from unbake.project.config import Held
from unbake.project_tools.rodata import fragment, placement, relocated


@dataclass(frozen=True)
class TrialObject:
    """Facts supplied by a trial; owner sets come from the split's reference graph."""

    obj: Any
    family: Any
    function: str
    text_address: int
    target_words: dict[int, int | None]
    read_memory: Callable[[int, int], bytes]
    owners: dict[tuple[str, int], tuple[str, ...]]


def needs(trial_obj: TrialObject, version: str) -> list[RodataNeed]:
    """Derive literal/table needs after proving every relocated constant byte."""
    for key in TrialObject.__dataclass_fields__:
        if not hasattr(trial_obj, key) or getattr(trial_obj, key) is None:
            raise Held("rodata", f"trial_obj.{key}: missing value")
    if not version:
        raise Held("rodata", "version: missing value")
    if not trial_obj.function:
        raise Held("rodata", "trial_obj.function: missing value")
    obj, family = trial_obj.obj, trial_obj.family
    grouped = [("jumptable", pool) for pool in family.jump_tables(obj)]
    grouped += [("literals", pool) for pool in family.literal_pools(obj)]
    bases = {}
    result = []
    for kind, pool in sorted(grouped, key=lambda item: (item[1].section, item[1].offset)):
        section = pool.section
        if section not in bases:
            try:
                base, dissent = placement(obj, section, trial_obj.target_words)
                data = relocated(obj, section, trial_obj.text_address)
            except ValueError as error:
                raise Held("rodata", str(error)) from error
            if base + len(data) > 0x100000000:
                raise Held("rodata", f"{section}.size: crosses address space")
            actual = trial_obj.read_memory(base, len(data))
            if actual != data:
                raise Held("rodata", f"{section}.bytes: disagree at 0x{base:08X}")
            bases[section] = base, dissent
        base, dissent = bases[section]
        pool_key = section, pool.offset
        if pool_key not in trial_obj.owners or not trial_obj.owners[pool_key]:
            raise Held("rodata", f"owners[{section},{pool.offset}]: missing reference owners")
        owners = trial_obj.owners[pool_key]
        if trial_obj.function not in owners:
            raise Held("rodata", f"owners[{section},{pool.offset}]: excludes {trial_obj.function}")
        evidence = dict(
            function=trial_obj.function,
            owners=list(owners),
            base=base,
            offset=pool.offset,
            dissent=dissent,
            section_size=len(obj.content(obj.section(section))),
        )
        result.append(RodataNeed(version, section, kind, base + pool.offset, pool.size, evidence))
    return result


def resolve(needs: list[Need], project: Any, policy: Any) -> list[split.Edit]:
    """Migrate exclusively owned pools; preserve shared resident split rows."""
    grouped: dict[Path, list[RodataNeed]] = {}
    for need in needs:
        if not isinstance(need, RodataNeed):
            raise Held("rodata", "need: expected RodataNeed")
        if need.section not in (".rdata", ".rodata") or need.kind not in ("jumptable", "literals"):
            raise Held("rodata", f"{need.section}.{need.kind}: unsupported constant pool")
        if not isinstance(need.evidence, dict):
            raise Held("rodata", "need.evidence: missing ownership evidence")
        for key in ("function", "owners", "base", "offset", "dissent", "section_size"):
            if key not in need.evidence or cast(dict[str, Any], need.evidence)[key] is None:
                raise Held("rodata", f"need.evidence.{key}: missing value")
        if not cast(dict[str, Any], need.evidence)["owners"]:
            raise Held("rodata", "need.evidence.owners: missing reference owners")
        if need.size <= 0:
            raise Held("rodata", "need.size: required positive byte count")
        version = project.version(need.version)
        grouped.setdefault(version.split, []).append(need)
    edits = []
    for path, selected in grouped.items():
        sections: dict[tuple[str, str, Any, Any], list[RodataNeed]] = {}
        for need in selected:
            section_key = (
                need.version,
                need.section,
                cast(dict[str, Any], need.evidence)["function"],
                cast(dict[str, Any], need.evidence)["base"],
            )
            sections.setdefault(section_key, []).append(need)
        prepared: list[RodataNeed] = []
        for section_needs in sections.values():
            if any(len(set(cast(dict[str, Any], need.evidence)["owners"])) > 1 for need in section_needs):
                owners = sorted(
                    {owner for need in section_needs for owner in cast(dict[str, Any], need.evidence)["owners"]}
                )
                prepared.extend(
                    RodataNeed(
                        need.version,
                        need.section,
                        need.kind,
                        need.address,
                        need.size,
                        {**cast(dict[str, Any], need.evidence), "owners": owners},
                    )
                    for need in section_needs
                )
                continue
            ordered = sorted(section_needs, key=lambda need: need.address)
            first = ordered[0]
            cursor = cast(dict[str, Any], first.evidence)["base"]
            for need in ordered:
                if need.address < cursor:
                    raise Held("rodata", f"{need.version}.{need.section}: overlapping pool needs")
                if (
                    need.address != cursor
                    or cast(dict[str, Any], need.evidence)["section_size"]
                    != cast(dict[str, Any], first.evidence)["section_size"]
                ):
                    raise Held("rodata", f"{need.version}.{need.section}: missing complete section needs")
                cursor += need.size
            if (
                cursor
                != cast(dict[str, Any], first.evidence)["base"] + cast(dict[str, Any], first.evidence)["section_size"]
            ):
                raise Held("rodata", f"{first.version}.{first.section}: missing complete section needs")
            prepared.append(
                RodataNeed(
                    first.version, first.section, first.kind, first.address, cursor - first.address, first.evidence
                )
            )
        selected = prepared
        before, lines, segments = split.layout(path)
        changes: dict[int, tuple[Any, list[tuple[RodataNeed, str]]]] = {}
        for need in selected:
            candidates = [
                row
                for segment in segments
                for row in segment.rows
                if row.kind in ("rodata", ".rodata", "rdata", ".rdata")
                and split.address(row, path) <= need.address
                and need.address + need.size <= split.address(row, path) + split.end(row) - row.start
            ]
            if len(candidates) != 1:
                raise Held(
                    "rodata", f"{need.version}.{need.section}@0x{need.address:08X}: required one resident split row"
                )
            row = candidates[0]
            if len(set(cast(dict[str, Any], need.evidence)["owners"])) > 1:
                if row.kind.startswith("."):
                    raise Held("rodata", f"{need.version}.{row.path}: shared pool requires resident rodata row")
                continue
            functions = [
                function
                for function in split.functions(project, need.version)
                if cast(dict[str, Any], need.evidence)["function"] in function.aliases
                or function.name == cast(dict[str, Any], need.evidence)["function"]
            ]
            if len(functions) != 1:
                raise Held(
                    "rodata",
                    f"{need.version}.{cast(dict[str, Any], need.evidence)['function']}: required one function row",
                )
            owner = functions[0].path
            if row.kind.startswith(".") and row.path == owner:
                continue
            if row.kind.startswith("."):
                raise Held("rodata", f"{need.version}.{row.path}: already migrated to another function")
            changes.setdefault(row.line, (row, []))[1].append((need, owner))
        for line, (row, intervals) in changes.items():
            output, cursor = [], row.start
            indent = row.match["indent"]
            for need, owner in sorted(intervals, key=lambda item: item[0].address):
                start = row.start + need.address - split.address(row, path)
                if start < cursor:
                    raise Held("rodata", f"{path}:{line + 1}: overlapping pool needs")
                if start > cursor:
                    output.append(f"{indent}- [0x{cursor:X}, {row.kind}, {json.dumps(row.path)}]\n")
                output.append(f"{indent}- [0x{start:X}, .rodata, {json.dumps(owner)}]\n")
                cursor = start + need.size
            if cursor < split.end(row):
                output.append(
                    f"{indent}- [0x{cursor:X}, {row.kind}, {json.dumps(row.path + '_at_' + format(cursor, 'X'))}]\n"
                )
            lines[line] = "".join(output)
        after = "".join(lines)
        if after != before:
            edits.append(split.Edit(Path(path), before, after, tuple(sorted({need.version for need in selected}))))
    return edits


def linker_fragment(rows: list[dict[str, Any]]) -> str:
    """Render shared resident overlays, refusing missing split-row facts by name."""
    try:
        return fragment(rows)
    except ValueError as error:
        raise Held("rodata", str(error)) from error


def derive(context: Any) -> list[Need]:
    """Collect VERSION-specific object evidence supplied by the trial."""
    from unbake.families import family_for
    from unbake.project_tools.elf import Object

    if not hasattr(context, "artifacts"):
        raise Held("rodata", "trial_ctx.artifacts: missing value")
    family = family_for(context.project.compiler_for(context.source))
    result: list[Need] = []
    for version, artifact in context.artifacts.items():
        if "unit" not in artifact:
            raise Held("rodata", f"artifacts.{version}.unit: missing value")
        obj = Object(artifact["unit"].path)
        if not family.literal_pools(obj) and not family.jump_tables(obj):
            continue
        if "rodata" not in artifact:
            raise Held("rodata", f"artifacts.{version}.rodata: missing TrialObject evidence")
        try:
            result.extend(needs(artifact["rodata"], version))
        except Held as error:
            if "placements" not in artifact or ".bytes: disagree" not in str(error):
                raise
            context.trial.preconditions.append(f"VERSION {version}: {error}")
            context.trial.compares[version].typed["rodata"] = 1
    return result


register_deriver(derive)
register_resolver(RodataNeed, 40, resolve)
