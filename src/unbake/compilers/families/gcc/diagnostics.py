"""Native GCC dump production, function slicing, annotation and allocation order."""

import re
from dataclasses import replace
from pathlib import Path
from typing import cast

from unbake import atomic as atomic_files
from unbake.compilers.families.types import Allocation, Pseudo, RegisterDifference, Schedule
from unbake.config import Held, Host, Project
from unbake.process import named as cause_named


def function_dump(text: str, function: str) -> str:
    """Select exactly one function, refusing absent or duplicate sections."""
    matches = list(re.finditer(r"^;; Function (\S+)(?:[ \t]+\([^\n]*\))?[ \t]*$", text, re.M))
    sections = [
        text[m.end() : matches[i + 1].start() if i + 1 < len(matches) else len(text)]
        for i, m in enumerate(matches)
        if m[1] == function
    ]
    if len(sections) != 1:
        raise Held(
            cause_named(
                f"dumps.function.{function}",
                f"dumps.function.{function}: expected one section, found {len(sections)}",
                owner="compilers.families.gcc.diagnostics",
                stage="explain",
            )
        )
    return sections[0]


def annotate(allocation: Allocation, rtl: str, source: str, expanded: str) -> Allocation:
    """Map RTL definitions through line notes to unique source assignments."""
    lines, originals = expanded.splitlines(), source.splitlines()
    found: dict[int, set[tuple[str, int]]] = {}
    line_number = None
    for line in rtl.splitlines():
        note = re.search(r'^\(note\s+\d+.*?\("[^\"]+"\)\s+(\d+)\)', line)
        if note:
            line_number = int(note[1])
        if line_number is None or not 1 <= line_number <= len(lines):
            continue
        expression = lines[line_number - 1].strip()
        locations = [i + 1 for i, value in enumerate(originals) if value.strip() == expression]
        if len(locations) != 1:
            continue
        definition = re.search(r"\(set \(reg(?:/v)?:\w+ (\d+)\)", line)
        assignment = re.match(r"\s*([A-Za-z_]\w*)\s*=(?!=)", expression)
        if definition and assignment:
            found.setdefault(int(definition[1]), set()).add((assignment[1], locations[0]))
    pseudos = []
    for pseudo in allocation.pseudos:
        evidence = found.get(pseudo.number, set())
        names = {name for name, _ in evidence}
        pseudos.append(
            replace(
                pseudo,
                name=next(iter(names)) if len(names) == 1 else None,
                source_lines=tuple(sorted(line for _, line in evidence)),
            )
        )
    return replace(allocation, pseudos=tuple(pseudos))


def allocation_hints(difference: RegisterDifference, by_number: dict[int, Pseudo]) -> list[str]:
    """For globally allocated pairs, say which side wins first and the change that reverses it."""
    from unbake.compilers.families.gcc.allocation import flip

    rows = []
    for candidate in (by_number[n] for n in difference.candidates):
        for holder in (by_number[n] for n in difference.holders):
            if any(
                p.words != 1 or sum(other.rank == p.rank for other in by_number.values()) > 1
                for p in (candidate, holder)
            ):
                continue
            facts = [(p.number, p.rank, p.references, p.live_length) for p in (candidate, holder)]
            ranked = [(n, r, refs, live) for n, r, refs, live in facts if None not in (r, refs, live)]
            if len(ranked) != 2:
                continue
            first, second = sorted(ranked, key=lambda row: cast(int, row[1]))
            rows.append(
                f"  allocation order: pseudo {first[0]} (rank {first[1]}) before "
                f"pseudo {second[0]} (rank {second[1]}); to put pseudo {second[0]} first: "
                + flip((cast(int, second[2]), cast(int, second[3])), (cast(int, first[2]), cast(int, first[3])))
            )
    return rows


def diagnostic_input(
    project: Project, policy: Host, source: Path, version: str, work: Path, *, preserve_lines: bool
) -> tuple[str, list[str]]:
    """The exact GCC input of the unit (NON_MATCHING defined), optionally with source line directives."""
    from unbake.compilers import drivers
    from unbake.decomp.explain import _absolute_includes

    compiler = project.compiler_for(source.stem)
    flags = _absolute_includes(project, drivers.flags(project, version, source.stem))
    _, codeflags = drivers.stage_flags(compiler.id, flags)
    command = drivers.preprocess_command(
        project, str(policy.cpp), version, source.stem, source, non_matching=True, line_markers=preserve_lines
    )
    expanded = drivers.run_preprocess(
        project,
        command,
        "preprocess",
        unit=source,
        temporary_root=project.build,
        context={"source": str(source), "version": version},
    )
    if not preserve_lines:
        expanded = re.sub(r"^\s*#\s*(?:line\s+)?\d+[^\n]*", "", expanded, flags=re.M)
    return expanded, list(codeflags)


def collect_allocation(project: Project, policy: Host, source: Path, version: str, work: Path) -> Allocation:
    import hashlib

    from unbake import runner
    from unbake.compilers import drivers
    from unbake.compilers.families.gcc import Gcc
    from unbake.process import run_tool
    from unbake.work.compare import row_of
    from unbake.work.score import words

    family = Gcc()
    expanded, _ = diagnostic_input(project, policy, source, version, work, preserve_lines=False)
    name = source.stem
    commands = drivers.steps(project, version, source, str(source), runner.tools(policy), non_matching=True)
    atomic_files.text(work / (name + ".i"), expanded)
    run_tool([str(project.compiler_for(source).cc), *family.dump_flags(), *commands.compile[1:]], work, "compile")
    if commands.assemble:
        run_tool(list(commands.assemble), work, "compile")
    linked, problems = runner.link_function(
        project, policy, work / (name + ".o"), version, row_of(project, name, version), source
    )
    if problems:
        raise Held(
            cause_named(
                "diagnostic.placement", "; ".join(problems), owner="compilers.families.gcc.diagnostics", stage="explain"
            )
        )
    dumps = {}
    for suffix in ("rtl", "lreg", "greg", "lalloc", "galloc", "combine", "jump", "jump2", "sched", "sched2", "dbr"):
        paths = sorted(work.glob("*." + suffix))
        if paths:
            dumps[suffix] = "\n".join(function_dump(path.read_text(), source.stem) for path in paths)
    result = family.allocation(dumps)
    parsed = family.compiler_facts(dumps, words(linked), expanded, name)
    origins = tuple(
        (offset, row["uid"], tuple(row["registers"]))
        for row in parsed["instructions"].values()
        for offset in row["candidate_offsets"]
    )
    result = replace(result, instruction_origins=origins, linked_sha256=hashlib.sha256(linked).hexdigest())
    return annotate(result, dumps["lreg"], source.read_text(), expanded)


def collect_schedule(project: Project, policy: Host, source: Path, version: str, work: Path) -> Schedule:
    from unbake.compilers.families.gcc import Gcc
    from unbake.process import run_tool

    family = Gcc()
    expanded, flags = diagnostic_input(project, policy, source, version, work, preserve_lines=False)
    input_path = work / "source.i"
    atomic_files.text(input_path, expanded, encoding="utf-8")
    run_tool(
        [
            str(project.compiler_for(source).cc),
            *flags,
            *family.dump_flags(),
            str(input_path),
            "-o",
            str(work / "source.s"),
        ],
        work,
        "explain",
    )
    dumps = {}
    for stage in ("sched2", "dbr"):
        paths = sorted(work.glob("*." + stage))
        if not paths:
            raise Held(
                cause_named(
                    f"dumps.{stage}",
                    f"dumps.{stage}: missing value",
                    owner="compilers.families.gcc.diagnostics",
                    stage="explain",
                )
            )
        dumps[stage] = "\n".join(function_dump(path.read_text(), source.stem) for path in paths)
    return family.schedule(dumps)
