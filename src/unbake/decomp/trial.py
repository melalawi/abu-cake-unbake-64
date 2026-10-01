"""Compile and compare a draft without mutating or locking a project build."""

from __future__ import annotations

import hashlib
import re
import shlex
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from unbake.decomp import checks, features
from unbake.decomp import needs as evidence
from unbake.decomp.commands import prefix
from unbake.decomp.trial_artifacts import Artifact, TrialContext, rodata_object
from unbake.decomp.trial_compare import TYPES, Compare, compare_words, words
from unbake.decomp.trial_compile import compile_draft, executable, run_tool, scratch_directory
from unbake.decomp.trial_layout import function_span, symbol_values, target
from unbake.decomp.trial_link import inspect, link, snapshot_layout
from unbake.decomp.trial_rodata import compare_rodata, pool_guidance, section_placements
from unbake.project import build
from unbake.project.config import Held, Policy, Project


@dataclass
class Trial:
    function: str
    source_sha256: str
    compares: dict[str, Compare]
    preconditions: list[str]
    next_command: str
    needs: list[evidence.Need] = field(default_factory=list)

    @property
    def identical_everywhere(self) -> bool:
        return bool(self.compares) and all(
            result.of > 0 and result.identical == result.of and not any(result.typed.values())
            for result in self.compares.values()
        )


def render(trial: Trial) -> str:
    lines = [f"OK(try): {trial.function} — NON_MATCHING draft"]
    for result in trial.compares.values():
        lines.extend(result.lines)
    lines.extend(f"need: {evidence.name(need)} ({type(need).__name__})" for need in trial.needs)
    lines.extend(f"precondition: {line}" for line in trial.preconditions)
    lines.append(f"next_command: {trial.next_command}")
    return "\n".join(lines)


def try_draft(
    project: Project, policy: Policy, source: Path, scratch: Path, versions: list[str] | None = None
) -> Trial:
    features.load()
    directory = scratch_directory(project, scratch, "try")
    if source is None or not str(source):
        raise Held("try", "source is required")
    source = Path(source).resolve()
    if source.suffix != ".c" or not re.fullmatch(r"[A-Za-z_]\w*", source.stem):
        raise Held("try", f"source {source} must be named <function>.c")
    try:
        content = source.read_bytes()
        text = content.decode("utf-8")
    except (OSError, UnicodeError) as error:
        raise Held("try", f"source {source}: {error}") from error
    findings = checks.run(text)
    preconditions = [checks.message(finding) for finding in findings if finding.fakematch is None]
    if any(finding.rule == "inline-asm" and finding.fakematch is None for finding in findings):
        raise Held("try", "; ".join(preconditions))
    selected = list(project.versions) if versions is None else list(versions)
    if not selected:
        raise Held("try", "versions is missing or empty")
    if len(set(selected)) != len(selected):
        raise Held("try", "versions contains duplicates")
    tools = {
        name: executable(getattr(policy, f"mips_{name}", None), f"mips_{name}", "try")
        for name in ("ld", "readelf", "objdump")
    }
    function = source.stem
    compares: dict[str, Compare] = {}
    trial = Trial(function, hashlib.sha256(content).hexdigest(), compares, preconditions, "")
    work = Path(tempfile.mkdtemp(prefix=f"{function}.", dir=directory))
    copied = work / source.name
    copied.write_bytes(b"#define NON_MATCHING 1\n" + content)
    for name in selected:
        version = project.version(name)
        values = symbol_values(Path(version.symbols))
        span = function_span(version, function, values)
        if span is None:
            if versions is not None:
                raise Held("try", f"{version.symbols}: function {function} is missing for VERSION {name}")
            continue
        generation = Path(build.current_generation(project, name))
        version_work = work / name
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", name):
            raise Held("try", f"VERSION {name!r} cannot name a scratch directory")
        version_work.mkdir()
        layout = inspect(snapshot_layout(generation, version_work), tools["readelf"], version_work)
        expected = target(version, span)
        object_path = version_work / f"{function}.o"
        compile_draft(project, policy, copied, name, object_path)
        unit = inspect(object_path, tools["readelf"], version_work)
        artifact: Artifact = {
            "unit": unit,
            "layout": layout,
            "target_words": words(expected),
            "span": span,
            "version": version,
            "work": version_work,
        }
        artifact["rodata"] = rodata_object(project, source, function, artifact, values)
        artifact["placements"] = section_placements(project, artifact, function)
        provisional = Compare(name, 0, len(expected) // 4, dict.fromkeys(TYPES, 0), pool_guidance(project, artifact))
        compares[name] = provisional
        pending = evidence.derive(TrialContext(project, policy, copied, trial, {name: artifact}))
        for need in pending:
            if need not in trial.needs:
                trial.needs.append(need)
        produced, relocations, linked = link(
            unit,
            layout,
            function,
            span,
            values,
            [*pending, *artifact["placements"]],
            tools["ld"],
            tools["readelf"],
            version_work,
        )
        (version_work / "baserom.bin").write_bytes(expected)
        (version_work / "draft.bin").write_bytes(produced)
        listing = run_tool([tools["objdump"], "-dr", str(linked)], version_work, "try")
        (version_work / "draft.asm").write_text(listing, encoding="utf-8")
        target_listing = run_tool(
            [
                tools["objdump"],
                "-D",
                "-b",
                "binary",
                "-m",
                "mips:4300",
                "-EB",
                f"--adjust-vma=0x{span.address:X}",
                str(version_work / "baserom.bin"),
            ],
            version_work,
            "try",
        )
        (version_work / "baserom.asm").write_text(target_listing, encoding="utf-8")
        comparison = compare_words(name, words(expected), words(produced), relocations)
        comparison.lines.insert(
            1, f"address 0x{span.address:08X}; ROM offset 0x{span.offset:X}; generation {generation}"
        )
        comparison.lines.extend(provisional.lines)
        if "rodata" in provisional.typed:
            comparison.typed["rodata"] = provisional.typed["rodata"]
        compare_rodata(artifact, linked, tools["readelf"], comparison)
        compares[name] = comparison
    if not compares:
        raise Held("try", f"function {function} is missing from symbols for versions {selected}")
    command = [*prefix(project), "decomp", "try", str(source), "--scratch", str(directory)]
    if versions is not None:
        for name in selected:
            command.extend(["--version", name])
    trial.next_command = shlex.join(command)
    if trial.identical_everywhere and not preconditions:
        if versions is not None and set(selected) != set(project.versions):
            trial.next_command = shlex.join(
                [*prefix(project), "decomp", "try", str(source), "--scratch", str(directory)]
            )
        else:
            trial.next_command = shlex.join([*prefix(project), "match", "submit", str(source)])
    (work / "report.txt").write_text(render(trial) + "\n", encoding="utf-8")
    print(render(trial))
    return trial
