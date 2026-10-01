"""Allocator evidence and explanations of aligned register differences."""

from __future__ import annotations

import re
import tempfile
from dataclasses import dataclass, replace
from pathlib import Path

from unbake.decomp.trial_compare import Compare
from unbake.families.gcc.schedule import Schedule
from unbake.project.config import Held, Policy, Project


@dataclass(frozen=True)
class Pseudo:
    number: int
    hard: int | None
    references: int | None
    live_length: int | None
    live_range: tuple[int, int] | None
    priority: float | None
    rank: int | None
    conflicts: tuple[int, ...]
    allocator: str
    source_lines: tuple[int, ...]
    name: str | None
    rejections: tuple[tuple[int, str], ...]


@dataclass(frozen=True)
class RegisterDifference:
    target_offset: int
    draft_offset: int
    target_hard: int
    draft_hard: int
    candidates: tuple[int, ...]
    holders: tuple[int, ...]
    ambiguous: bool


@dataclass(frozen=True)
class Allocation:
    pseudos: tuple[Pseudo, ...]
    differences: tuple[RegisterDifference, ...]
    limitations: tuple[str, ...]
    hard_registers: tuple[int, ...]


def function_dump(text: str, function: str) -> str:
    """Select exactly one function, refusing absent or duplicate sections."""
    matches = list(re.finditer(r"^;; Function (\S+)\s*$", text, re.M))
    sections = [
        text[m.end() : matches[i + 1].start() if i + 1 < len(matches) else len(text)]
        for i, m in enumerate(matches)
        if m[1] == function
    ]
    if len(sections) != 1:
        raise Held("explain", f"dumps.function.{function}: expected one section, found {len(sections)}")
    return sections[0]


def align(allocation: Allocation, comparison: Compare) -> Allocation:
    """Attach the trial's aligned register words to possible pseudos and winners."""
    from unbake.decomp.trial_compare import fields

    differences = []
    pattern = re.compile(r"^register: target \+0x([0-9A-F]+) ([0-9A-F]{8}); draft \+0x([0-9A-F]+) ([0-9A-F]{8})$", re.I)
    for line in comparison.lines:
        match = pattern.fullmatch(line)
        if match is None:
            continue
        target_offset, target, draft_offset, draft = (int(x, 16) for x in match.groups())
        mask, _ = fields(target)
        for shift in (6, 11, 16, 21):
            field = 31 << shift
            if mask & field != field or not (target ^ draft) & field:
                continue
            before, after = (target >> shift) & 31, (draft >> shift) & 31
            if target >> 26 == 0x11:
                mode = (target >> 21) & 31
                if shift in (6, 11) or (shift == 16 and mode >= 16):
                    before, after = before + 32, after + 32
            candidates = tuple(p.number for p in allocation.pseudos if p.hard == after)
            holders = tuple(p.number for p in allocation.pseudos if p.hard == before)
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
    by_number = {p.number: p for p in allocation.pseudos}
    for difference in allocation.differences:
        rows.append(
            f"+0x{difference.target_offset:04X}: register {difference.draft_hard} wants "
            f"{difference.target_hard}; ambiguous={difference.ambiguous}"
        )
        for role, numbers in (("candidate", difference.candidates), ("holder", difference.holders)):
            for number in numbers:
                p = by_number[number]
                rows.append(
                    f"  {role} pseudo {number}: priority={p.priority} refs={p.references} "
                    f"live={p.live_range} length={p.live_length} allocator={p.allocator}"
                )
                rows.extend(
                    f"    rejected {hard}: {why}" for hard, why in p.rejections if hard == difference.target_hard
                )
    if not allocation.differences:
        rows.append("No aligned register differences; no register leverage found.")
    return "\n".join(rows)


def allocation(project: Project, policy: Policy, source: Path, version: str) -> Allocation:
    """Compile private diagnostic streams and explain the normal compiler trial."""
    from unbake.decomp import trial, trial_compile
    from unbake.families import family_for
    from unbake.project import makefile, toolchain

    source = Path(source).resolve()
    if not source.is_file():
        raise Held("explain", f"source.{source}: missing file")
    project.version(version)
    compiler = project.compiler_for(source)
    spec = toolchain.specification(compiler.id)
    if spec.family not in ("gcc", "ido"):
        raise Held("explain", f"compiler.{compiler.id}.family: unsupported {spec.family}")
    family = family_for(compiler.id)
    root = _state_root(policy) / "explain"
    trial_compile.scratch_directory(project, root, "explain")
    flags = list(makefile.flags(project, version, source))
    # The pinned native cc1 writes the allocator dumps itself; diagnostics use the exact build compiler.
    selected = compiler
    toolchain.verify(project.tools / selected.id, toolchain.specification(selected.id))
    with tempfile.TemporaryDirectory(prefix=source.stem + ".", dir=root) as temporary:
        work = Path(temporary)
        comparison = trial.try_draft(project, policy, source, work, versions=[version]).compares[version]
        if spec.family == "gcc":
            expanded, codeflags = _gcc_input(project, policy, source, version, work)
            input_path = work / "source.i"
            input_path.write_text(expanded)
            trial_compile.run_tool(
                [
                    str(selected.cc),
                    *codeflags,
                    *family.dump_flags(),
                    "-g",
                    str(input_path),
                    "-o",
                    str(work / "source.s"),
                ],
                work,
                "explain",
            )
            dumps = {}
            for suffix in ("lreg", "greg", "lalloc", "galloc"):
                paths = sorted(work.glob("*." + suffix))
                if paths:
                    dumps[suffix] = "\n".join(function_dump(p.read_text(), source.stem) for p in paths)
            result = family.allocation(dumps)
            if "lreg" not in dumps:
                raise Held("explain", "dumps.lreg: missing value")
            result = annotate(result, dumps["lreg"], source.read_text(), expanded)
        else:
            flags = _absolute_includes(project, flags)
            trial_compile.run_tool(
                [str(selected.cc), *flags, *family.dump_flags(), str(source), "-o", str(work / "source.s")],
                work,
                "explain",
            )
            listing = work / "source.s"
            if not listing.is_file():
                raise Held("explain", "dumps.ido: -K emitted no textual assignment listing")
            result = family.allocation({"ido": listing.read_text()})
        return align(result, comparison)


def _absolute_includes(project: Project, flags: list[str] | tuple[str, ...]) -> list[str]:
    values = list(flags)
    for index, flag in enumerate(values):
        if flag in ("-I", "-include", "-isystem"):
            if index + 1 >= len(values):
                raise Held("explain", f"compiler.cflags.{flag}: missing value")
            path = Path(values[index + 1])
            if not path.is_absolute():
                values[index + 1] = str(project.root / path)
        elif flag.startswith("-I") and len(flag) > 2 and not Path(flag[2:]).is_absolute():
            values[index] = "-I" + str(project.root / flag[2:])
    return values


def _state_root(policy: Policy) -> Path:
    root = getattr(policy, "state_root", None)
    if root is None:
        raise Held("explain", "policy.state_root: missing value")
    return Path(root)


def _gcc_input(project: Project, policy: Policy, source: Path, version: str, work: Path) -> tuple[str, list[str]]:
    from unbake.decomp import trial_compile
    from unbake.project import makefile
    from unbake.project_tools.sn64_cc import partition_flags

    compiler = project.compiler_for(source)
    flags = _absolute_includes(project, makefile.flags(project, version, source))
    if compiler.kind == "sn64":
        try:
            options, codeflags = partition_flags(flags)
        except ValueError as error:
            raise Held("explain", f"compiler.cflags: {error}") from error
        recipe = makefile.recipe(project)
        command = [makefile.host_executable(policy, recipe.cpp or "", "cpp"), *recipe.cppflags, *options, str(source)]
    else:
        codeflags = [flag for flag in flags if flag != "-c"]
        command = [str(compiler.cc), *codeflags, "-E", str(source)]
    expanded = trial_compile.run_tool(command, work, "explain")
    expanded = re.sub(r"^\s*#\s*(?:line\s+)?\d+[^\n]*", "", expanded, flags=re.M)
    return expanded, codeflags


def order(project: Project, policy: Policy, source: Path, version: str) -> Schedule:
    """Read family scheduling evidence for one selected unit and VERSION."""
    from unbake.decomp import trial_compile
    from unbake.families import family_for
    from unbake.project import toolchain

    source = Path(source).resolve()
    if not source.is_file():
        raise Held("explain", f"source.{source}: missing file")
    project.version(version)
    compiler = project.compiler_for(source)
    family = family_for(compiler.id)
    spec = toolchain.specification(compiler.id)
    if spec.family == "ido":
        return family.schedule(None)
    toolchain.verify(project.tools / compiler.id, spec)
    root = trial_compile.scratch_directory(project, _state_root(policy) / "explain", "explain")
    with tempfile.TemporaryDirectory(prefix=source.stem + ".", dir=root) as temporary:
        work = Path(temporary)
        expanded, flags = _gcc_input(project, policy, source, version, work)
        input_path = work / "source.i"
        input_path.write_text(expanded, encoding="utf-8")
        trial_compile.run_tool(
            [str(compiler.cc), *flags, *family.dump_flags(), str(input_path), "-o", str(work / "source.s")],
            work,
            "explain",
        )
        dumps = {}
        for stage in ("sched2", "dbr"):
            paths = sorted(work.glob("*." + stage))
            if not paths:
                raise Held("explain", f"dumps.{stage}: missing value")
            dumps[stage] = "\n".join(function_dump(path.read_text(), source.stem) for path in paths)
        return family.schedule(dumps)
