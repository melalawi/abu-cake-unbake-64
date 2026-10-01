"""Compile drafts and compare relocatable objects from the current build."""

from __future__ import annotations

import hashlib
import json
import re
import shlex
import tempfile
from dataclasses import dataclass
from pathlib import Path

from unbake.decomp import checks
from unbake.decomp.commands import prefix
from unbake.decomp.score import diff
from unbake.decomp.trial_compare import Compare, compare_object
from unbake.decomp.trial_compile import compile_draft, run_tool, scratch_directory
from unbake.decomp.trial_source import annotate_divergence
from unbake.decomp.trial_target import target_object
from unbake.project import build
from unbake.project.config import Held, Policy, Project
from unbake.project_tools.elf import Object


@dataclass
class Trial:
    function: str
    source_sha256: str
    compares: dict[str, Compare]
    preconditions: list[str]
    next_command: str

    @property
    def identical_everywhere(self) -> bool:
        return bool(self.compares) and all(
            result.match_percent == 100 and not any(result.typed.values()) for result in self.compares.values()
        )


def render(trial: Trial) -> str:
    lines = [f"OK(try): {trial.function} — NON_MATCHING draft"]
    for result in trial.compares.values():
        lines.extend(result.lines)
    lines.extend(f"precondition: {line}" for line in trial.preconditions)
    lines.append(f"next_command: {trial.next_command}")
    return "\n".join(lines)


def try_draft(
    project: Project, policy: Policy, source: Path, scratch: Path, versions: list[str] | None = None
) -> Trial:
    directory = scratch_directory(project, scratch, "try")
    source = Path(source).resolve()
    if source.suffix != ".c" or not re.fullmatch(r"[A-Za-z_]\w*", source.stem):
        raise Held("try", f"source {source} must be named <function>.c")
    try:
        content = source.read_bytes()
        text = content.decode("utf-8")
    except (OSError, UnicodeError) as error:
        raise Held("try", f"source {source}: {error}") from error
    preconditions = [checks.message(finding) for finding in checks.run(text) if finding.fakematch is None]
    selected = list(project.versions) if versions is None else list(versions)
    if not selected or len(set(selected)) != len(selected):
        raise Held("try", "versions must be nonempty and unique")
    function = source.stem
    trial = Trial(function, hashlib.sha256(content).hexdigest(), {}, preconditions, "")
    work = Path(tempfile.mkdtemp(prefix=f"{function}.", dir=directory))
    copied = work / source.name
    copied.write_bytes(("#define NON_MATCHING 1\n#line 1 " + json.dumps(str(source)) + "\n").encode() + content)
    for name in selected:
        project.version(name)
        generation = build.current_generation(project, name)
        target = target_object(generation, function, name)
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
        candidate = version_work / f"{function}.o"
        compile_draft(project, policy, copied, name, candidate)
        comparison = compare_object(
            name, diff(policy, name, function, target, candidate, version_work / "objdiff.json"), function
        )
        annotate_divergence(project, policy, copied, source, name, version_work, candidate, comparison)
        comparison.lines.insert(1, f"target object {target}; generation {generation}")
        trial.compares[name] = comparison
    command = [*prefix(project), "decomp", "try", str(source), "--scratch", str(directory)]
    if versions is not None:
        for name in selected:
            command.extend(["--version", name])
    trial.next_command = shlex.join(command)
    if trial.identical_everywhere and not preconditions:
        trial.next_command = shlex.join(
            [*prefix(project), "match", "submit", str(source)]
            if set(selected) == set(project.versions)
            else [*prefix(project), "decomp", "try", str(source), "--scratch", str(directory)]
        )
    (work / "report.txt").write_text(render(trial) + "\n", encoding="utf-8")
    print(render(trial))
    return trial
