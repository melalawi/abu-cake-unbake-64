"""Compile drafts and compare relocatable objects from the current build."""

from __future__ import annotations

import json
import re
import shlex
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from unbake.decomp import checks, drafts
from unbake.decomp import work as draft_work
from unbake.decomp.commands import prefix
from unbake.decomp.score import diff
from unbake.decomp.trial_compare import Compare, compare_object
from unbake.decomp.trial_compile import compile_draft, run_tool, scratch_directory
from unbake.decomp.trial_flags import FlagResult, compiler_variants, ranking, variant_project
from unbake.decomp.trial_source import annotate_divergence
from unbake.decomp.trial_target import inputs as trial_inputs
from unbake.decomp.trial_target import owning_versions, require_symbol_boundary
from unbake.project.config import Held, Policy, Project
from unbake.project_tools.elf import Object


@dataclass
class Trial:
    function: str
    source_sha256: str
    compares: dict[str, Compare]
    preconditions: list[str]
    next_command: str
    flag_results: list[FlagResult] = field(default_factory=list)
    generations: dict[str, Path] = field(default_factory=dict)
    work_identity: dict[str, object] = field(default_factory=dict)

    @property
    def identical_everywhere(self) -> bool:
        return bool(self.compares) and all(comparison_identical(result) for result in self.compares.values())


def comparison_identical(result: Compare) -> bool:
    """Use instruction identity after resolving relocation names by address."""
    return result.of > 0 and result.identical == result.of and not any(result.typed.values())


def render(trial: Trial) -> str:
    lines = [f"OK(try): {trial.function} — NON_MATCHING draft"]
    for result in trial.compares.values():
        lines.extend(result.lines)
    if trial.flag_results:
        lines.extend(ranking(trial.flag_results, list(trial.compares)))
    lines.extend(f"precondition: {line}" for line in trial.preconditions)

    return "\n".join(lines)


def store_trial(project: Project, policy: Policy, source: Path, result: Trial) -> None:
    """Retain evidence measured against pinned, immutable build generations."""
    from unbake.decomp import drafts

    drafts.Store(policy, project).add(
        result, source, {v: 100 if comparison_identical(c) else c.match_percent for v, c in result.compares.items()}
    )


def _source_content(source: Path) -> tuple[bytes, str]:
    if source.suffix != ".c" or not re.fullmatch(r"[A-Za-z_]\w*", source.stem):
        raise Held("try", f"source {source} must be named <function>.c")
    try:
        content = source.read_bytes()
        return content, content.decode("utf-8")
    except (OSError, UnicodeError) as error:
        raise Held("try", f"source {source}: {error}") from error


def retain_draft(
    project: Project,
    policy: Policy,
    source: Path,
    scratch: Path,
    versions: list[str] | None,
    *,
    flags: bool = False,
) -> Trial:
    """Compare and retain while pinning generations, without holding the writer lock."""
    directory = scratch_directory(project, scratch.expanduser(), "decomp")
    source = source.resolve()
    _source_content(source)
    work = Path(tempfile.mkdtemp(prefix=f"{source.stem}-", dir=directory))
    selected = owning_versions(project, source.stem, versions)
    with trial_inputs(project, source.stem, selected) as pinned:
        before = draft_work.identity(project, source, selected, pinned=pinned, policy=policy)
        result = (
            try_draft(project, policy, source, work, versions=versions, flags=True, pinned=pinned)
            if flags
            else try_draft(project, policy, source, work, versions=versions, pinned=pinned)
        )
        after = draft_work.identity(project, source, selected, pinned=pinned, policy=policy)
        if before != after:
            raise Held("try", "trial.inputs_changed: inputs changed during compilation; try again")
        result.work_identity = dict(after)
        draft_work.persist(project, after)
        store_trial(project, policy, source, result)
    from unbake.cli.common import suggest

    suggest(result.next_command)
    return result


def try_draft(
    project: Project,
    policy: Policy,
    source: Path,
    scratch: Path,
    versions: list[str] | None = None,
    *,
    flags: bool = False,
    pinned: dict[str, tuple[Path, Path]] | None = None,
) -> Trial:
    directory = scratch_directory(project, scratch, "try")
    source = Path(source).resolve()
    content, text = _source_content(source)
    original_project = project
    project = draft_work.compilation_project(project, source)
    variants = compiler_variants(project, source) if flags else [()]
    preconditions = [f"{source}:{checks.message(finding)}" for finding in checks.run(text) if finding.fakematch is None]
    selected = owning_versions(project, source.stem, versions) if pinned is None else list(pinned)
    function = source.stem
    if pinned is None:
        with trial_inputs(original_project, function, selected) as pinned:
            return try_draft(original_project, policy, source, scratch, versions, flags=flags, pinned=pinned)
    for name, (_, target) in pinned.items():
        require_symbol_boundary(project, function, name, target)
    trial = Trial(function, drafts.source_identity(content), {}, preconditions, "")
    results = [FlagResult(variant, {}, {}) for variant in variants]
    if flags:
        trial.flag_results = results
    work = Path(tempfile.mkdtemp(prefix=f"{function}.", dir=directory))
    copied = work / source.name
    copied.write_bytes(("#define NON_MATCHING 1\n#line 1 " + json.dumps(str(source)) + "\n").encode() + content)
    for name in selected:
        project.version(name)
        generation, target = pinned[name]
        trial.generations[name] = generation
        version_work = work / name
        version_work.mkdir()
        obj = Object(target)
        symbols = [s for table in obj.symbols.values() for s in table if s["info"] & 15 == 2 and s["section"]]
        if not any(s["name"] == function for s in symbols):
            entries = [s for s in symbols if s["value"] == 0]
            if len(entries) != 1:
                raise Held("try", f"VERSION {name}: target object entry for {function} is missing")
            canonical = version_work / "target.o"
            run_tool(
                [
                    str(policy.mips_objcopy),
                    "--redefine-sym",
                    entries[0]["name"] + "=" + function,
                    str(target),
                    str(canonical),
                ],
                version_work,
                "try",
            )
            target = canonical
        for index, result in enumerate(results):
            variant_work = version_work / f"flags-{index}" if flags else version_work
            variant_work.mkdir(exist_ok=True)
            candidate = variant_work / f"{function}.o"
            configured = variant_project(project, copied, result.flags) if result.flags else project
            try:
                compile_draft(configured, policy, copied, name, candidate)
            except Held as error:
                if index == 0:
                    raise
                result.failures[name] = error.reason
                continue
            comparison = compare_object(
                name,
                diff(policy, name, function, target, candidate, variant_work / "objdiff.json", generation=generation),
                function,
            )
            result.compares[name] = comparison
            if index == 0:
                if not flags:
                    annotate_divergence(project, policy, copied, source, name, version_work, candidate, comparison)
                comparison.lines.insert(1, f"target object {target}; generation {generation}")
                trial.compares[name] = comparison
    command = [*prefix(original_project), "try", str(source)]
    if flags:
        command.append("--flags")
    trial.next_command = f"Edit {source}. Then run " + shlex.join(command)
    if trial.identical_everywhere and not preconditions:
        trial.next_command = shlex.join(
            [*prefix(original_project), "submit", str(source)]
            if set(selected) == set(owning_versions(original_project, function, None))
            else [*prefix(original_project), "try", str(source)]
        )
    (work / "report.txt").write_text(render(trial) + "\n", encoding="utf-8")
    print(render(trial))
    return trial
