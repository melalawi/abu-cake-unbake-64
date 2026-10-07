"""The `headers` step: regenerate group headers and source include lines from layout.toml and the type solution.

- Carry installed declaration dependencies used by published C; refuse incompatible proven evidence by name.
- Only files whose bytes change are written; the index is installed last (layout.apply.install).
- Before writing, every affected unit is compiled for each version that builds its C, against staged copies of
  the changed headers and sources in build/work/_headers/ (a draft view shadowing include/ by relative name).
- Each symbol has one declaration, in its owning header. A source's local declaration of a header-declared
  symbol is removed; where its type differs, the unit must still match the ROM with the header form.
"""

from __future__ import annotations

import re
import shutil
import tempfile
from dataclasses import replace
from pathlib import Path
from typing import Any

from unbake import atomic as atomic_files
from unbake import cache, inputs
from unbake.config import Held, Host, Project
from unbake.journal import Journal
from unbake.layout import apply, index, split
from unbake.process import capture
from unbake.process import named as cause_named
from unbake.project.headers import Graph, HeaderCheck, scan

# Bump when this step's output changes for the same inputs. Keys never digest the tool's code.
SCHEMA = 8


def input_key(project: Project) -> str:
    """layout.toml, the installed type solution, every header and every published source.

    The solution is the types step's output, not its input key: a solve that changes nothing reruns nothing here."""
    from unbake.typemap import types_db

    paths = (
        project.root / "layout.toml",
        *(path for include in project.include for path in sorted(include.rglob("*.h"))),
        *sorted(project.src.rglob("*.c")),
    )
    dependencies = inputs.DependencySet(
        tuple(inputs.file_pin(path, root=project.root, root_id="project", reuse=cache.configured()) for path in paths),
        {"solution": types_db.solution(types_db.path(project))},
        {"header-inputs": inputs.digest(Path(__file__), algorithm="sha256", reuse=cache.configured())},
    )
    return cache.key("headers", str(SCHEMA), dependencies.digest)


def plan(project: Project, outputs: dict[Path, bytes], host: Host) -> tuple[dict[Path, bytes], HeaderCheck]:
    """The outputs whose bytes differ from the tree, after the merge-only check.

    A previously generated header the outputs no longer contain is deleted by apply.install, so it is
    checked like a change to an empty header."""
    changed = {path: data for path, data in outputs.items() if not path.is_file() or path.read_bytes() != data}
    from unbake.layout import header_loss

    check = header_loss.check(project, outputs, obsolete=index.owned(project) - outputs.keys(), policy=host)
    return changed, check


_DEFINED = re.compile(r"^[A-Za-z_][\w \t*]*?\b([A-Za-z_]\w*)[ \t]*\([^;{]*\)[ \t\n]*\{", re.M)


def uses(text: str) -> set[str]:
    """Names a source spells, except a function it only defines: its definition needs no header declaration."""
    spelled = apply.spelled(text)
    code = re.sub(r"/\*.*?\*/|//[^\n]*", " ", text, flags=re.S)
    only_defined = {
        name for name in _DEFINED.findall(code) if len(re.findall(r"\b" + re.escape(name) + r"\b", code)) == 1
    }
    return spelled - only_defined


def _compile(job: tuple[Project, Host, Path, str, str, bool, bool]) -> dict[str, Any] | None:
    """Compile one staged unit for one version, retaining its complete refusal."""
    from unbake import runner

    view, host, file, version, unit, prove_match, non_matching = job
    try:
        if prove_match and not non_matching:
            data = runner.build_unit(view, host, unit, version, source=file)
            rows = [row for row in split.functions(view, version) if Path(row.path).name == unit and row.kind == "c"]
            if len(rows) != 1 or data != split.words(view, rows[0]):
                raise Held(
                    cause_named(
                        "headers.nonregression",
                        f"headers.nonregression: {unit} VERSION {version}: changed default ROM code",
                        owner="layout.header_step",
                        stage="headers",
                    )
                )
        else:
            with runner.compile_unit(view, host, file, version, unit=unit, non_matching=non_matching):
                pass
    except Held as error:
        return {
            "key": error.key,
            "reason": f"VERSION {version}: {error.reason}",
            "fault": capture(
                error,
                cause=cause_named(
                    "layout.header_step.unexpected", str(error), owner="layout.header_step", stage="layout"
                ),
            ).document(),
        }
    return None


def validate(
    project: Project,
    host: Host,
    changed: dict[Path, bytes],
    disagreements: dict[Path, dict[str, tuple[str, str]]] | None = None,
    *,
    prove_all: bool = False,
    preproved: frozenset[str] = frozenset(),
    obsolete: frozenset[Path] = frozenset(),
) -> list[str]:
    """Compile every unit the change reaches against staged copies, on all cores; return the units compiled.

    A source whose local declaration lost to its header's differing form must still match the ROM in every
    published C version with the header form; otherwise every such symbol is refused by name."""
    from unbake import pool
    from unbake.work import attempts

    project.work.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=".headers-", dir=project.work))
    staged_headers = stage / "include"
    staged_sources = stage / "src"
    headers = {path.resolve() for path in changed if path.suffix == ".h"} | {path.resolve() for path in obsolete}
    roots: set[Path] = set()
    for path, data in {**changed, **dict.fromkeys(obsolete, b"")}.items():
        if path.suffix == ".h":
            root = next((r for r in project.include if path.is_relative_to(r)), None)
            if root is None:
                raise Held(
                    cause_named(
                        "headers.path",
                        f"headers.path: {path} is outside include/",
                        owner="layout.header_step",
                        stage="headers",
                    )
                )
            atomic_files.write(staged_headers / path.relative_to(root), data)
            roots.add(root)
    # A staged header's quoted includes ("../types.h") resolve beside it, so the rest of its tree is linked in.
    for root in sorted(roots):
        for path in root.rglob("*"):
            mirror = staged_headers / path.relative_to(root)
            if path.is_file() and not mirror.exists():
                mirror.parent.mkdir(parents=True, exist_ok=True)
                mirror.symlink_to(path)
    before = Graph.capture(project)
    after = Graph(before.view.overlay({**dict.fromkeys(obsolete), **changed}), before.search)
    affected = set(after.affected(before, headers | set(changed), project.src.rglob("*.c")))
    # Each VERSION's alias index once, not once per affected source.
    owners = {version: split.owners_by_alias(project, version) for version in project.versions}
    view = replace(project, work_include=(staged_headers,))
    compiled = []
    published: dict[Path, tuple[str, ...]] = {}
    jobs: list[tuple[Project, Host, Path, str, str, bool, bool]] = []
    fuzzy = attempts.ledger(project).fuzzy_sources()
    try:
        for source in sorted(project.src.glob("*.c")):
            if source.stem in preproved:
                continue
            own = changed.get(source)
            if own is None and source not in (disagreements or {}) and source not in affected:
                continue
            published[source] = tuple(
                version
                for version in split.holding_versions(project, source.stem, owners)
                if source.stem in fuzzy or any(row.kind == "c" for row in owners[version].get(source.stem, ()))
            )
            if not published[source]:
                continue
            file = source
            if own is not None:
                file = staged_sources / source.name
                atomic_files.write(file, own)
            jobs.extend(
                (view, host, file, version, source.stem, prove_all, source.stem in fuzzy)
                for version in published[source]
            )
            compiled.append(source.stem)
        # Every unit compiles; all that fail are refused together, not one per run.
        failures = tuple(failure for failure in pool.run(host, _compile, jobs) if failure is not None)
        if failures:
            reason = failures[0]["reason"]
            keys = ", ".join(sorted({failure["key"].removeprefix("compile.") for failure in failures}))
            raise Held(
                cause_named(
                    "compile.headers",
                    (
                        f"compile.headers: {len(failures)} unit "
                        f"{('default-build proofs' if prove_all else 'compiles')} fail against "
                        f"the regenerated headers ({keys}); first: {reason}"
                    ),
                    owner="layout.header_step",
                    stage="compile",
                ),
                failures=failures,
            )
        proofs = (
            []
            if prove_all
            else sorted(
                (source, found)
                for source, found in (disagreements or {}).items()
                if published.get(source) and source.stem not in fuzzy
            )
        )
        matched = pool.run(
            host,
            _prove_published,
            [
                (view, host, source, changed.get(source, source.read_bytes()).decode(), published[source])
                for source, _ in proofs
            ],
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
        raise Held(
            cause_named(
                "layout.header_step.validate",
                "headers.declaration: " + "; ".join(refused),
                owner="layout.header_step",
                stage="headers",
            )
        )
    return compiled


def _prove_published(job: tuple[Project, Host, Path, str, tuple[str, ...]]) -> bool:
    from unbake.layout import merge_units

    project, host, source, text, versions = job
    return merge_units.prove(project, host, (source.stem,), text, versions=versions)


def run(project: Project, host: Host) -> list[Path]:
    """Regenerate, validate the affected units, then write the changed files; return them.

    All or nothing: every path the step writes or deletes is journaled first (journal.py), so a hold, an
    exception or a killed process leaves the tree as it was."""
    with Journal(journal_path(project), root=project.root) as changes:
        changes.save(project.version(version).split for version in project.versions)
        apply.units(project)
        disagreements: dict[Path, dict[str, tuple[str, str]]] = {}
        outputs = apply.render(project, host, disagreements)
        changed, check = plan(project, outputs, host)
        obsolete = index.owned(project) - outputs.keys()
        if not changed and not obsolete:
            return []
        validate(project, host, changed, disagreements, obsolete=frozenset(obsolete))
        # install needs every output: a generated header missing from them is deleted as obsolete.
        changes.save([*changed, *obsolete])
        publish(project, dict(outputs), changes, check=check)
        return sorted(set(changed) | obsolete)


def missing(project: Project) -> list[str]:
    """Generated headers (listed by the index or homed by a layout group) that published sources include and the
    tree lacks."""
    from unbake.layout import map as layout_map

    root = project.include[0]
    generated = {path.relative_to(root).as_posix() for path in index.listed(project)}
    generated |= {group.header for group in layout_map.load(project).groups}
    absent = set()
    for source in project.src.glob("*.c"):
        for directive in scan(source.read_text(errors="replace")):
            name = directive.name
            if name in generated and not (root / name).is_file():
                absent.add(name)
    return sorted(absent)


def journal_path(project: Project) -> Path:
    return project.build / "headers.journal"


def publish(project: Project, outputs: dict[Path, bytes | Path], transaction: Journal, *, check: HeaderCheck) -> int:
    """The sole generated-header/index installer; caller supplies the owning transaction."""
    obsolete = index.owned(project) - outputs.keys()
    changed = {
        p
        for p, data in outputs.items()
        if not p.is_file() or p.read_bytes() != (data.read_bytes() if isinstance(data, Path) else data)
    }
    transaction.save([*changed, *obsolete])
    return apply.install(project, outputs, check=check)
