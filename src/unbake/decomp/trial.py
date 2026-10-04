"""Compile drafts and compare relocatable objects from the current build."""

from __future__ import annotations

import json
import re
import shlex
import tempfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import cast

from unbake.decomp import checks, drafts, trial_entries
from unbake.decomp import work as draft_work
from unbake.decomp.commands import prefix
from unbake.decomp.score import diff
from unbake.decomp.trial_compare import Compare, compare_object, words
from unbake.decomp.trial_compile import compile_draft, run_tool, scratch_directory
from unbake.decomp.trial_flags import FlagResult, compiler_variants, ranking, variant_project
from unbake.decomp.trial_source import annotate_divergence
from unbake.decomp.trial_target import inputs as trial_inputs
from unbake.decomp.trial_target import owning_versions
from unbake.layout import entries as entry_layout
from unbake.project.config import Held, Policy, Project, relative_text
from unbake.project_tools import atomic as atomic_files
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
    overlay_root: Path | None = None,
) -> Trial:
    """Compare and retain while pinning generations, without holding the writer lock."""
    directory = scratch_directory(project, scratch, "decomp")
    policy = replace(policy, state_root=directory / "state", cache_root=directory / "cache")
    source = source.resolve()
    original = source
    authored, text = _source_content(source)
    from unbake.decomp import gbi_recover

    gbi_recover.preflight(project, policy, source, text)
    work = Path(tempfile.mkdtemp(prefix=f"{source.stem}-", dir=directory))
    if overlay_root is not None:
        source = draft_work.overlay_source(project, source, work / "authored", overlay_root)
    prepared = draft_work.trial_view(project, policy, source, work / "prepared")
    selected = owning_versions(project, source.stem, versions)
    has_groups = any(len(entry_layout.owners(project, policy, source, v)) > 1 for v in selected)
    inputs = (
        trial_inputs(project, source.stem, selected, source=source, policy=policy, scratch=work, read_only=True)
        if has_groups
        else trial_inputs(project, source.stem, selected, read_only=True)
    )
    with inputs as pinned:
        from unbake.decomp.trial_compilers import resolve

        before = draft_work.identity(project, source, selected, pinned=pinned, policy=policy)
        configured: Trial | Held
        try:
            configured = try_draft(
                project, policy, prepared, work, versions=versions, flags=flags, pinned=pinned, function=source.stem
            )
        except Held as error:
            if error.phase != "compile" or flags:
                raise
            configured = error
        measured, compiler_evidence, result = (
            (project, {}, configured)
            if flags and isinstance(configured, Trial)
            else resolve(project, policy, source, prepared, work, pinned, configured)
        )
        after = draft_work.identity(project, source, selected, pinned=pinned, policy=policy)
        if after != before:
            raise Held("try", "trial.inputs_changed: inputs changed during compilation; try again")
        if measured is not project:
            project = measured
            after = draft_work.identity(project, source, selected, pinned=pinned, policy=policy)
        after["compiler_evidence"] = compiler_evidence
        result.work_identity = dict(after)
        if original.read_bytes() != authored or source.read_bytes() != authored:
            raise Held("try", "trial.inputs_changed: authored source changed during preparation")
        result.source_sha256 = drafts.source_identity(authored)
        result.next_command = (
            result.next_command.replace(str(prepared), str(source)) + " --scratch " + shlex.quote(str(directory))
        )
        atomic_files.write(work / "manifest.json", draft_work.encoded(after))
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
    function: str | None = None,
) -> Trial:
    directory = scratch_directory(project, scratch, "try")
    source = Path(source).resolve()
    content, text = _source_content(source)
    original_project = project
    project = draft_work.compilation_project(project, source)
    variants = compiler_variants(project, source) if flags else [()]
    preconditions = [f"{source}:{checks.message(finding)}" for finding in checks.run(text) if finding.fakematch is None]
    selected = owning_versions(project, function or source.stem, versions) if pinned is None else list(pinned)
    function = function or source.stem
    if pinned is None:
        has_groups = any(len(entry_layout.owners(project, policy, source, v)) > 1 for v in selected)
        inputs = (
            trial_inputs(original_project, function, selected, source=source, policy=policy)
            if has_groups
            else trial_inputs(original_project, function, selected)
        )
        with inputs as pinned:
            return try_draft(
                original_project, policy, source, scratch, versions, flags=flags, pinned=pinned, function=function
            )
    trial = Trial(function, drafts.source_identity(content), {}, preconditions, "")
    results = [FlagResult(variant, {}, {}) for variant in variants]
    if flags:
        trial.flag_results = results
    work = Path(tempfile.mkdtemp(prefix=f"{function}.", dir=directory))
    copied = work / f"{function}.c"
    atomic_files.write(copied, ("#define NON_MATCHING 1\n#line 1 " + json.dumps(str(source)) + "\n").encode() + content)

    def compile_variant(name: str, index: int) -> Held | None:
        result = results[index]
        variant_work = work / name / f"flags-{index}" if flags else work / name
        variant_work.mkdir(parents=True, exist_ok=True)
        configured = variant_project(project, copied, result.flags) if result.flags else project
        try:
            compile_draft(configured, policy, copied, name, variant_work / f"{function}.o")
        except Held as error:
            return error
        return None

    jobs = [(name, index) for name in selected for index in range(len(results))]
    with ThreadPoolExecutor(max_workers=min(policy.cores, len(jobs))) as pool:
        futures = {(name, index): pool.submit(compile_variant, name, index) for name, index in jobs}
        compile_errors = {job: future.result() for job, future in futures.items()}
    for name in selected:
        project.version(name)
        generation, target = pinned[name]
        trial.generations[name] = generation
        version_work = work / name
        version_work.mkdir(exist_ok=True)
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
            try:
                compile_error = compile_errors[name, index]
                if compile_error is not None:
                    raise compile_error
                compiled = Object(candidate)
                if not any(
                    symbol["name"] == function and symbol["section"] and symbol["info"] & 15 == 2
                    for table in compiled.symbols.values()
                    for symbol in table
                ):
                    raise Held(
                        "try", f"function {function}: compiled definition missing in source {source} VERSION {name}"
                    )
            except Held as error:
                if index == 0:
                    raise
                result.failures[name] = error.reason
                continue
            target_view, candidate_view, entry_failures = trial_entries.comparison_views(
                project, policy, source, name, target, candidate, variant_work
            )
            from unbake.decomp.trial_data import infer

            inferred = {}
            try:
                inferred = infer(project, policy, function, name, candidate)
            except Held as error:
                if index == 0:
                    trial.preconditions.append("trial.data_symbols: " + error.reason)
            document = diff(
                policy,
                name,
                function,
                target_view,
                candidate_view,
                variant_work / "objdiff.json",
                generation=generation,
                placement_target=target,
                inferred_addresses=inferred,
            )
            comparison = compare_object(name, document, function)
            comparison.typed["changed"] += len(entry_failures)
            comparison.lines.extend("entry: " + failure for failure in entry_failures)
            if index == 0:
                trial.preconditions.extend(f"trial.entries.{name}: {failure}" for failure in entry_failures)
            if not comparison_identical(comparison):
                comparison.lines.extend(
                    f"placement: {reason}" for reason in cast(list[str], document.get("placement_refusals", []))
                )
            comparison.target_words = _function_words(target_view, function)
            comparison.candidate_words = _function_words(candidate_view, function)
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
    atomic_files.text(work / "report.txt", relative_text(project.root, render(trial)) + "\n", encoding="utf-8")
    print(render(trial))
    return trial


def _function_words(path: Path, function: str) -> tuple[int, ...]:
    obj = Object(path)
    symbol = next(s for table in obj.symbols.values() for s in table if s["name"] == function and s["section"])
    data = obj.content(symbol["section"])
    return tuple(words(data[symbol["value"] : symbol["value"] + symbol["size"]]))
