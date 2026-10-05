"""The `headers` step: regenerate group headers and source include lines from layout.toml and the type solution.

- Merge-only: a header change or deletion that would remove a name published C spells is refused by name.
- Only files whose bytes change are written; the index is installed last (layout.apply.install).
- Before writing, every affected unit is compiled for each version that holds it, against staged copies of
  the changed headers and sources in build/work/_headers/ (a draft view shadowing include/ by relative name).
- Each symbol has one declaration, in its owning header. A source's local declaration of a header-declared
  symbol is removed; where its type differs, the unit must still match the ROM with the header form.
"""

from __future__ import annotations

import re
import shutil
from dataclasses import replace
from pathlib import Path

from unbake import atomic as atomic_files
from unbake import cache
from unbake.config import Held, Host, Project
from unbake.journal import Journal
from unbake.layout import apply, index, split

# Bump when this step's output changes for the same inputs. Keys never digest the tool's code.
SCHEMA = 2

_INCLUDE = re.compile(r'^[ \t]*#[ \t]*include[ \t]*[<"]([^>"\n]+)[>"]', re.M)
_DECLARED = (
    re.compile(r"^[ \t]*#[ \t]*define[ \t]+(\w+)", re.M),
    re.compile(r"\b(?:struct|union|enum)[ \t]+(\w+)[ \t]*\{"),
    re.compile(r"\b(\w+)[ \t]*(?:\[[^\]]*\])*[ \t]*;[ \t]*(?://[^\n]*)?$", re.M),
    re.compile(r"\b(\w+)[ \t]*\([^;{]*\)[ \t]*;"),
)


def input_key(project: Project) -> str:
    """layout.toml, the installed type solution, every header and every published source.

    The solution is the types step's output, not its input key: a solve that changes nothing reruns nothing here."""
    from unbake.typemap import types_db

    parts: list[str | bytes | Path] = ["headers", str(SCHEMA), project.root / "layout.toml"]
    parts.append(types_db.solution(types_db.path(project)) or "no solution")
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
        used |= uses(source.read_text())
    obsolete = {path: b"" for path in index.headers(project) - outputs.keys()}
    # A name that moves to another generated header is not removed.
    kept = set().union(*(declared(data.decode()) for path, data in outputs.items() if path.suffix == ".h"))
    refusals = []
    for path in sorted({**changed, **obsolete}):
        if path.suffix != ".h" or not path.is_file():
            continue
        removed = (declared(path.read_text()) - kept) & used
        if removed:
            refusals.append(f"{path}: would remove {', '.join(sorted(removed))} used by published C")
    if refusals:
        raise Held("headers", "headers.merge_only: " + "; ".join(refusals))
    return changed


_DEFINED = re.compile(r"^[A-Za-z_][\w \t*]*?\b([A-Za-z_]\w*)[ \t]*\([^;{]*\)[ \t\n]*\{", re.M)


def uses(text: str) -> set[str]:
    """Names a source spells, except a function it only defines: its definition needs no header declaration."""
    spelled = apply.spelled(text)
    code = re.sub(r"/\*.*?\*/|//[^\n]*", " ", text, flags=re.S)
    only_defined = {
        name for name in _DEFINED.findall(code) if len(re.findall(r"\b" + re.escape(name) + r"\b", code)) == 1
    }
    return spelled - only_defined


def _compile(job: tuple[Project, Host, Path, str, str]) -> None:
    """Worker body: compile one staged unit for one version."""
    from unbake import runner

    view, host, file, version, unit = job
    runner.compile_unit(view, host, file, version, unit=unit)


def validate(
    project: Project,
    host: Host,
    changed: dict[Path, bytes],
    disagreements: dict[Path, dict[str, tuple[str, str]]] | None = None,
) -> list[str]:
    """Compile every unit the change reaches against staged copies, on all cores; return the units compiled.

    A source whose local declaration lost to its header's differing form must still match the ROM in every
    holding version with the header form; otherwise every such symbol is refused by name."""
    from unbake import pool
    from unbake.layout import merge_units

    stage = project.work / "_headers"
    shutil.rmtree(stage, ignore_errors=True)
    staged_headers = stage / "include"
    staged_sources = stage / "src"
    headers = {path.resolve() for path in changed if path.suffix == ".h"}
    roots: set[Path] = set()
    for path, data in changed.items():
        if path.suffix == ".h":
            root = next((r for r in project.include if path.is_relative_to(r)), None)
            if root is None:
                raise Held("headers", f"headers.path: {path} is outside include/")
            atomic_files.write(staged_headers / path.relative_to(root), data)
            roots.add(root)
    # A staged header's quoted includes ("../types.h") resolve beside it, so the rest of its tree is linked in.
    for root in sorted(roots):
        for path in root.rglob("*"):
            mirror = staged_headers / path.relative_to(root)
            if path.is_file() and not mirror.exists():
                mirror.parent.mkdir(parents=True, exist_ok=True)
                mirror.symlink_to(path)
    memo: dict[Path, frozenset[Path]] = {}
    # Each VERSION's alias index once, not once per affected source.
    owners = {version: split.owners_by_alias(project, version) for version in project.versions}
    view = replace(project, work_include=(staged_headers,))
    compiled = []
    jobs: list[tuple[Project, Host, Path, str, str]] = []
    try:
        for source in sorted(project.src.glob("*.c")):
            own = changed.get(source)
            if own is None and not (includes(source.resolve(), project, memo) & headers):
                continue
            file = source
            if own is not None:
                file = staged_sources / source.name
                atomic_files.write(file, own)
            jobs.extend(
                (view, host, file, version, source.stem)
                for version in split.holding_versions(project, source.stem, owners)
            )
            compiled.append(source.stem)
        pool.run(host, _compile, jobs)
        proofs = sorted((disagreements or {}).items())
        matched = pool.run(
            host,
            merge_units.prove_job,
            [(view, host, (source.stem,), changed.get(source, source.read_bytes()).decode()) for source, _ in proofs],
        )
        refused = [
            f"{name} in {source.name}: local `{local}` matched the ROM; header `{header}` does not"
            for (source, found), passed in zip(proofs, matched, strict=True)
            if not passed
            for name, (local, header) in sorted(found.items())
        ]
    finally:
        shutil.rmtree(stage, ignore_errors=True)
    if refused:
        raise Held("headers", "headers.declaration: " + "; ".join(refused))
    return compiled


def run(project: Project, host: Host) -> list[Path]:
    """Regenerate, validate the affected units, then write the changed files; return them.

    All or nothing: every path the step writes or deletes is journaled first (journal.py), so a hold, an
    exception or a killed process leaves the tree as it was."""
    from unbake import lock

    # Every write, the rollback included, happens inside the publish section that cycle readers respect.
    with Journal(journal_path(project), section=lambda: lock.publishing(project.root)) as changes:
        changes.save(project.version(version).split for version in project.versions)
        with lock.publishing(project.root):
            apply.units(project)
        disagreements: dict[Path, dict[str, tuple[str, str]]] = {}
        outputs = apply.render(project, host, disagreements)
        changed = plan(project, outputs)
        if not changed:
            return []
        validate(project, host, changed, disagreements)
        # install needs every output: a generated header missing from them is deleted as obsolete.
        changes.save([*changed, *(index.headers(project) - outputs.keys())])
        apply.install(project, dict(outputs))
        return sorted(changed)


def missing(project: Project) -> list[str]:
    """Generated headers (listed by the index or homed by a layout group) that published sources include and the
    tree lacks."""
    from unbake.layout import map as layout_map

    root = project.include[0]
    generated = {path.relative_to(root).as_posix() for path in index.listed(project)}
    generated |= {group.header for group in layout_map.load(project).groups}
    absent = set()
    for source in project.src.glob("*.c"):
        for name in _INCLUDE.findall(source.read_text(errors="replace")):
            if name in generated and not (root / name).is_file():
                absent.add(name)
    return sorted(absent)


def absent(project: Project, functions: tuple[str, ...]) -> list[str]:
    """Generated headers a draft context of FUNCTIONS includes and the tree lacks: the ones published sources and
    present headers include (a draft context includes every header), and the group headers of FUNCTIONS."""
    from unbake.layout import map as layout_map

    root = project.include[0]
    groups = layout_map.load(project).groups
    generated = {path.relative_to(root).as_posix() for path in index.listed(project)}
    generated |= {group.header for group in groups}
    wanted = {group.header for group in groups if set(group.members) & set(functions)}
    texts = [*project.src.glob("*.c"), *(path for include in project.include for path in include.rglob("*.h"))]
    for path in texts:
        wanted.update(_INCLUDE.findall(path.read_text(errors="replace")))
    return sorted(name for name in wanted & generated if not (root / name).is_file())


def journal_path(project: Project) -> Path:
    return project.build / "headers.journal"
