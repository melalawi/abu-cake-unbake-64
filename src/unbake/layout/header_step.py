"""The `headers` step: regenerate group headers and source include lines from layout.toml and the type solution.

- Merge-only: a header change or deletion that would remove a name published C spells is refused by name.
- Only files whose bytes change are written; the index is installed last (layout.apply.install).
- Before writing, every affected unit is compiled for each version that holds it, against staged copies of
  the changed headers and sources in build/work/_headers/ (a draft view shadowing include/ by relative name).
"""

from __future__ import annotations

import re
import shutil
from dataclasses import replace
from pathlib import Path

from unbake import atomic as atomic_files
from unbake import cache, steps
from unbake.config import Held, Host, Project
from unbake.layout import apply, index, split

_INCLUDE = re.compile(r'^[ \t]*#[ \t]*include[ \t]*[<"]([^>"\n]+)[>"]', re.M)
_DECLARED = (
    re.compile(r"^[ \t]*#[ \t]*define[ \t]+(\w+)", re.M),
    re.compile(r"\b(?:struct|union|enum)[ \t]+(\w+)[ \t]*\{"),
    re.compile(r"\b(\w+)[ \t]*(?:\[[^\]]*\])*[ \t]*;[ \t]*(?://[^\n]*)?$", re.M),
    re.compile(r"\b(\w+)[ \t]*\([^;{]*\)[ \t]*;"),
)


def input_key(project: Project) -> str:
    """layout.toml, the recorded type solution, every authored header and every published source."""
    parts: list[str | bytes | Path] = [steps.tool_fingerprint(), project.root / "layout.toml"]
    parts.append(steps.recorded(project, "types") or "")
    for include in project.include:
        parts.extend(sorted(include.rglob("*.h")))
    parts.extend(sorted(project.src.glob("*.c")))
    return cache.key(*parts)


def declared(text: str) -> set[str]:
    return {name for pattern in _DECLARED for name in pattern.findall(text)}


def includes(path: Path, project: Project, memo: dict[Path, frozenset[Path]]) -> frozenset[Path]:
    """Every header PATH includes, transitively, resolved against include/ (unresolved names are skipped)."""
    if path in memo:
        return memo[path]
    memo[path] = frozenset()
    found: set[Path] = set()
    for name in _INCLUDE.findall(path.read_text(errors="replace")):
        for root in (path.parent, *project.include):
            candidate = (root / name).resolve()
            if candidate.is_file():
                found.add(candidate)
                found |= includes(candidate, project, memo)
                break
    memo[path] = frozenset(found)
    return memo[path]


def plan(project: Project, outputs: dict[Path, bytes]) -> dict[Path, bytes]:
    """The outputs whose bytes differ from the tree, after the merge-only check.

    A previously generated header the outputs no longer contain is deleted by apply.install, so it is
    checked like a change to an empty header."""
    changed = {path: data for path, data in outputs.items() if not path.is_file() or path.read_bytes() != data}
    used: set[str] = set()
    for source in project.src.glob("*.c"):
        used |= apply.spelled(source.read_text())
    obsolete = {path: b"" for path in index.headers(project) - outputs.keys()}
    for path, data in {**changed, **obsolete}.items():
        if path.suffix != ".h" or not path.is_file():
            continue
        removed = (declared(path.read_text()) - declared(data.decode())) & used
        if removed:
            raise Held(
                "headers", f"headers.merge_only: {path}: would remove {', '.join(sorted(removed))} used by published C"
            )
    return changed


def validate(project: Project, host: Host, changed: dict[Path, bytes]) -> list[str]:
    """Compile every unit the change reaches against staged copies; return the units compiled."""
    from unbake import runner

    stage = project.work / "_headers"
    shutil.rmtree(stage, ignore_errors=True)
    staged_headers = stage / "include"
    staged_sources = stage / "src"
    headers = {path.resolve() for path in changed if path.suffix == ".h"}
    for path, data in changed.items():
        if path.suffix == ".h":
            root = next((r for r in project.include if path.is_relative_to(r)), None)
            if root is None:
                raise Held("headers", f"headers.path: {path} is outside include/")
            atomic_files.write(staged_headers / path.relative_to(root), data)
    memo: dict[Path, frozenset[Path]] = {}
    view = replace(project, work_include=(staged_headers,))
    compiled = []
    try:
        for source in sorted(project.src.glob("*.c")):
            own = changed.get(source)
            if own is None and not (includes(source.resolve(), project, memo) & headers):
                continue
            file = source
            if own is not None:
                file = staged_sources / source.name
                atomic_files.write(file, own)
            for version in split.holding_versions(project, source.stem):
                runner.compile_unit(view, host, file, version, unit=source.stem)
            compiled.append(source.stem)
    finally:
        shutil.rmtree(stage, ignore_errors=True)
    return compiled


def run(project: Project, host: Host) -> list[Path]:
    """Regenerate, validate the affected units, then write the changed files; return them."""
    apply.units(project)
    outputs = apply.render(project, host)
    changed = plan(project, outputs)
    if not changed:
        return []
    validate(project, host, changed)
    # install needs every output: a generated header missing from them is deleted as obsolete.
    apply.install(project, dict(outputs))
    return sorted(changed)
