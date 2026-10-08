"""land(F): fold, prove the publication versions against the ROM, write, commit "Match F".

An already published unit lands the same way (its row edits are a no-op) and commits "Clean F".

Proof: every ROM piece other than F's row is either a raw ROM slice or an already matched unit, so the ROM of a
version is byte-identical to the original exactly when F's linked .text (strict `n64link place`, every constant
proved) equals the ROM bytes of F's row. The default requires every holding version. An explicit required scope
also proves every already published version and exact freebie; the other versions retain assembly.
Header text that fold appends is staged first; every published unit that includes a changed header is compiled
against the staged copy (layout.header_step.validate) before the write.
"""

from __future__ import annotations

import hashlib
import shutil
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import toml  # type: ignore[import-untyped]

from unbake import atomic as atomic_files
from unbake import buildfiles, inputs, journal, process, runner, scratch, steps
from unbake import cache as retention
from unbake.cache import Cache
from unbake.config import Held, Host, Project
from unbake.layout import split
from unbake.process import Fault, capture
from unbake.process import named as cause_named
from unbake.project.headers import scan
from unbake.tui import progress as tui
from unbake.work import attempts, compare


@dataclass
class Landed:
    landed: list[str] = field(default_factory=list)
    commits: list[str] = field(default_factory=list)
    failed: dict[str, dict[str, Any]] = field(default_factory=dict)
    versions: dict[str, list[str]] = field(default_factory=dict)
    post_commit_failure: dict[str, Any] | None = None
    interrupted: bool = False
    ready: list[str] = field(default_factory=list)

    def document(self) -> dict[str, Any]:
        return {
            "landed": self.landed,
            "commits": self.commits,
            "failed": self.failed,
            "versions": self.versions,
            **({"post_commit_failure": self.post_commit_failure} if self.post_commit_failure is not None else {}),
            **({"ready": self.ready, "retryable": True} if self.interrupted else {}),
        }

    def lines(self) -> list[str]:
        out = [f"landed {name} ({commit[:12]})" for name, commit in zip(self.landed, self.commits, strict=True)]
        out += [f"not landed {name}: {failure['reason']}" for name, failure in self.failed.items()]
        if self.post_commit_failure is not None:
            out.append(f"after commit {self.post_commit_failure['commit']}: {self.post_commit_failure['reason']}")
        return out


def _git(project: Project, *args: str, env: dict[str, str] | None = None) -> str:
    argv, stdin = process.git_pathspec(["git", *args])
    return process.run_native(argv, project.root, "land", temporary_root=project.build, env=env, stdin=stdin).stdout


def dirty(project: Project) -> set[str]:
    """Paths git reports changed or untracked, relative to the project root."""
    rows = _git(project, "status", "--porcelain", "--untracked-files=all", "-z").split("\0")
    paths = set()
    skip = False
    for row in rows:
        if skip or not row:
            skip = False
            continue
        if row[0] in "RC":
            skip = True  # the next field is the rename's source
        paths.add(row[3:])
    return paths


def snapshot(project: Project) -> dict[str, str | None]:
    """Every dirty path with a digest of its current bytes (None when deleted)."""
    return {
        path: hashlib.sha256((project.root / path).read_bytes()).hexdigest()
        if (project.root / path).is_file()
        else None
        for path in dirty(project)
    }


def commit_generated(project: Project, host: Host, before: dict[str, str | None], message: str) -> str | None:
    """Commit what the tool changed since BEFORE (the snapshot at its start); None when nothing did.

    A path dirty at the start counts as changed when its bytes differ now: a header an earlier step left dirty and
    this run rewrote must travel with the files that include or are included by it."""
    now = snapshot(project)
    changed = sorted(path for path, digest in now.items() if path not in before or before[path] != digest)
    if not changed:
        return None
    _commit(project, host, [project.root / path for path in changed], message)
    return _git(project, "rev-parse", "HEAD").strip()


def dangling_includes(project: Project, names: list[str]) -> list[tuple[str, str]]:
    from unbake.layout import headers
    from unbake.project.headers import Graph, Search, TreeView

    view = TreeView.git(project, names)
    graph = Graph(view, Search(include_roots=tuple(project.include)))
    disk = Graph.capture(project)
    dangling = []
    for path in sorted(view.files):
        for include in scan(view.read(path).decode(errors="replace")):
            if include.unknown:
                raise Held(
                    cause_named(
                        "land.include_unknown",
                        f"land.include_unknown: {path}: native dependency proof required",
                        owner="land",
                        stage="land",
                    )
                )
            resolution = graph.resolve(path, include)
            if resolution.target is not None:
                continue
            project_header = disk.resolve(path, include).target is not None or headers.shared_header(include.name)
            if project_header:
                dangling.append((path.relative_to(project.root).as_posix(), include.name))
    return sorted(set(dangling))


def _refuse_dangling_includes(project: Project, names: list[str]) -> None:
    dangling = dangling_includes(project, names)
    if dangling:
        raise Held(
            cause_named(
                "land._refuse_dangling_includes",
                "commit.include: refusing a commit whose tree has a file including a project header it lacks: "
                + "; ".join((f'{file} includes "{header}"' for file, header in dangling)),
                owner="land",
                stage="land",
            )
        )


def _commit(project: Project, host: Host, paths: list[Path], message: str) -> None:
    with journal.transaction(project) as transaction:
        storage = attempts.storage_paths(project)
        if storage:
            paths = [*paths, *buildfiles.write_progress(project, publish_branch=host.publish_branch)]
        paths = sorted({*paths, *storage})
        names = [str(path.relative_to(project.root)) for path in paths]
        _refuse_dangling_includes(project, names)
        index = Path(_git(project, "rev-parse", "--git-path", "index").strip())
        if not index.is_absolute():
            index = project.root / index
        author = f"{host.publish_author_name} <{host.publish_author_email}>"
        trailer = transaction.prepare_commit(project, host, index, paths, _git(project, "rev-parse", "HEAD").strip())
        _git(project, "add", "--", *names)
        _git(
            project,
            "-c",
            f"user.name={host.publish_author_name}",
            "-c",
            f"user.email={host.publish_author_email}",
            "commit",
            "-q",
            "-m",
            message + "\n\n" + trailer,
            "--author",
            author,
            "--only",
            "--",
            *names,
        )
        journal.accepted(git_commit=_git(project, "rev-parse", "HEAD").strip())


def record(project: Project, host: Host) -> tuple[str, tuple[str, ...]] | None:
    """Cycle end: fold the attempt logs into attempts.json and commit it with the reports it moves.

    Return the "Record attempts" commit and the functions whose history it records, or None when no attempt
    changed attempts.json (report changes alone are committed with the other generated files)."""
    from unbake.report import progress

    ledger_path = project.root / attempts.PATH
    changed = _git(project, "status", "--porcelain", "--", attempts.PATH).strip()
    if not changed:
        return None
    history = attempts.ledger(project).summaries()
    functions = tuple(sorted(history))
    paths = progress.write(project, host)
    steps.record(project, "progress", steps.STEPS["progress"].key(project, host))
    _commit(project, host, sorted({*paths, ledger_path}), "Record attempts: " + ", ".join(functions))
    return _git(project, "rev-parse", "HEAD").strip(), functions


def publication_versions(
    project: Project, function: str, attempt: attempts.Attempt, required: tuple[str, ...]
) -> tuple[str, ...]:
    """Explicit requirements, already published C, and exact freebies; no version is removed from the project."""
    holding = split.holding_versions(project, function)
    if not required or len(set(required)) != len(required) or set(required) - set(holding):
        raise Held(
            cause_named(
                "land.versions",
                f"land.versions: {function}: require distinct holding versions from {', '.join(holding)}",
                owner="land",
                stage="land",
            )
        )
    mandatory = set(required) | {v for v in holding if compare.row_of(project, function, v).kind == "c"}
    from unbake.work.score import Measurement

    try:
        exact = {v for v in holding if v in attempt.versions and Measurement.read(attempt.versions[v]).exact}
    except ValueError as error:
        raise Held(
            cause_named(
                "land.historical_unverified",
                "migrate-state and compare again for current proof",
                owner="land",
                stage="admission",
            )
        ) from error
    if missing := mandatory - exact:
        comparison_refusal(function, attempt, tuple(v for v in holding if v in missing))
        raise Held(
            cause_named(
                "land.publication_versions",
                f"land.not_exact: {function}: required or published versions are not exact: "
                + ", ".join(v for v in holding if v in missing),
                owner="land",
                stage="land",
            )
        )
    return tuple(v for v in holding if v in exact)


def comparison_refusal(function: str, attempt: attempts.Attempt, versions: tuple[str, ...]) -> None:
    """An unavailable comparison transports its owning fault through exact admission."""
    for version in versions:
        measured = attempt.versions.get(version, {})
        if measured.get("fault"):
            fault = Fault.read(measured["fault"])
            raise Held(
                fault.framed(
                    "land",
                    "admission",
                    f"{function} VERSION {version}: exact comparison unavailable",
                    {"version": version, "source_sha256": attempt.sha256},
                )
            )
        if measured.get("percent") is None:
            raise Held(
                cause_named(
                    "land.comparison_unavailable",
                    f"{function} VERSION {version}: comparison unavailable without retained native evidence",
                    owner="land",
                    stage="admission",
                    subject=function,
                    evidence={"version": version, "source_sha256": attempt.sha256, "native_fault": None},
                )
            )


def _attempt_recipe(project: Project, function: str, attempt: attempts.Attempt) -> Project:
    from unbake.compilers.recipe_options import UnitRecipe

    recipes = [
        UnitRecipe.read(row["provenance"]["unit_recipe"])
        for row in attempt.versions.values()
        if row.get("available") and row.get("provenance", {}).get("unit_recipe")
    ]
    if not recipes or any(recipe != recipes[0] for recipe in recipes):
        raise Held(
            cause_named(
                "land.recipe",
                "complete consistent measured UnitRecipe required; compare again",
                owner="land",
                stage="admission",
            )
        )
    return replace(project, units={**project.units, project.unit_path(function): recipes[0]})


def exact_attempt(
    project: Project, host: Host, function: str, file: Path, *, required_versions: tuple[str, ...] | None = None
) -> attempts.Attempt:
    """One strict predicate plus current source/recipe/target/dependency readback."""
    from unbake.compilers.drivers import resolved
    from unbake.decomp import checks
    from unbake.work.score import Measurement

    digest = hashlib.sha256(file.read_bytes()).hexdigest()
    found = [row for row in attempts.ledger(project).history(function) if row.sha256 == digest]
    if not found:
        raise Held(
            cause_named(
                "land.not_compared",
                f"{function}: compare the current source before publication",
                owner="land",
                stage="admission",
            )
        )
    attempt = found[-1]
    comparison_refusal(function, attempt, required_versions or split.holding_versions(project, function))
    view = _attempt_recipe(project, function, attempt)
    selected = (
        publication_versions(view, function, attempt, required_versions)
        if required_versions is not None
        else split.holding_versions(view, function)
    )
    try:
        measurements = {v: Measurement.read(value) for v, value in attempt.versions.items()}
    except ValueError as error:
        raise Held(
            cause_named(
                "land.historical_unverified",
                "current complete proof required; migrate-state and compare again",
                owner="land",
                stage="admission",
            )
        ) from error
    broken = [checks.plain(row.finding) for row in checks.findings(project, (file,), Cache(project.cache)).unmarked]
    if not compare.acceptance(measurements, selected, broken):
        raise Held(
            cause_named(
                "land.not_exact",
                f"{function}: publication requires complete strict native proof and source rules",
                owner="land",
                stage="admission",
            )
        )
    dependencies = compare.operation_dependencies(compare.view_for(view, file, function), host, file)
    for version in selected:
        result = measurements[version]
        proof = result.provenance
        target = split.words(view, compare.row_of(view, function, version))
        if (
            proof["source_sha256"] != digest
            or proof["recipe_digest"] != resolved(view, version, function).digest
            or proof["dependency_digest"] != dependencies.digest
            or proof["target_sha256"] != hashlib.sha256(target).hexdigest()
            or not set(selected) <= set(proof.get("required_versions", []))
        ):
            raise Held(
                cause_named(
                    "land.stale_proof",
                    f"{function} VERSION {version}: current inputs differ; compare again",
                    owner="land",
                    stage="admission",
                    evidence={
                        "current_dependencies": dependencies.digest,
                        "measured_dependencies": proof.get("dependency_digest"),
                    },
                )
            )
    return attempt


def _with_compiler(project: Project, function: str, ident: str) -> Project:
    from unbake.compilers.choice import selected

    return selected(project, function, ident)


def _unit_config(project: Project, function: str, ident: str, before: bytes) -> bytes:
    from unbake.compilers.recipe_options import UnitRecipe

    data = toml.loads(before.decode())
    units = dict(data.get("units", {}))
    previous = project.recipe_for(function)
    units[project.unit_path(function)] = UnitRecipe(ident, previous.options, previous.functions).document()
    if units == data.get("units", {}):
        return before
    data["units"] = dict(sorted(units.items()))
    return str(toml.dumps(data)).encode()


def _compiler_config(
    project: Project, function: str, ident: str, before: bytes, *, staged_before: bytes | None = None
) -> bytes:
    """Project only the proved unit onto HEAD; unrelated staged unit edits remain local."""
    path = project.root / "config.toml"
    if path.read_bytes() != before:
        raise Held(
            cause_named("land.config", "land.config: config.toml changed since proof", owner="land", stage="land")
        )
    data = toml.loads(before.decode())
    if "config.toml" in dirty(project):
        committed_bytes = _git(project, "show", "HEAD:config.toml").encode()
        committed = toml.loads(committed_bytes.decode())
        staged_bytes = _git(project, "show", ":config.toml").encode()
        if staged_before is not None and staged_bytes != staged_before:
            raise Held(cause_named("land.config", "config index changed since proof", owner="land", stage="land"))
        staged = toml.loads(staged_bytes.decode())

        def other_units(document: dict[str, Any]) -> dict[str, Any]:
            return {name: row for name, row in document.get("units", {}).items() if name != project.unit_path(function)}

        if (
            {key: value for key, value in data.items() if key != "units"}
            != {key: value for key, value in committed.items() if key != "units"}
            or {key: value for key, value in staged.items() if key != "units"}
            != {key: value for key, value in committed.items() if key != "units"}
            or other_units(data) != other_units(staged)
        ):
            raise Held(
                cause_named(
                    "land.config",
                    f"land.config: {function}: unproved config changes outside its unit options",
                    owner="land",
                    stage="land",
                )
            )
        before = committed_bytes
    return _unit_config(project, function, ident, before)


def _restore_compiler_config(project: Project, host: Host, local: bytes, staged: bytes) -> None:
    """Restore unrelated config work through the existing journal and Git index boundary."""
    path = project.root / "config.toml"
    if path.read_bytes() == local and _git(project, "show", ":config.toml").encode() == staged:
        return
    index = Path(_git(project, "rev-parse", "--git-path", "index").strip())
    if not index.is_absolute():
        index = project.root / index
    with journal.transaction(project) as transaction:
        transaction.save([path, index])
        atomic_files.write(path, local)
        with scratch.temporary(host, project, "land", prefix="config-index-") as temporary:
            image = Path(temporary) / "config.toml"
            atomic_files.write(image, staged)
            blob = _git(project, "hash-object", "-w", str(image)).strip()
            _git(project, "update-index", "--cacheinfo", "100644", blob, "config.toml")


def _builds_row(spec: tuple[Project, Project, Host, str, Path, str]) -> tuple[bool, set[Path]]:
    """Pool worker: one version's compile, place and link of the staged unit equals its ROM row."""
    project, view, host, function, file, version = spec
    row = compare.row_of(project, function, version)
    with (
        runner.compile_unit(view, host, file, version, unit=function) as obj,
        scratch.temporary(host, project, "land", prefix="land-") as temporary,
    ):
        work = Path(temporary)
        placed = work / "placed.o"
        runner.place(project, host, obj, version, row, placed, score=False)
        linked = runner.link(project, host, placed, version, row, work, file, obj)
    equal = linked == split.words(project, row)
    return equal, runner.dependencies(view, host, file, version, unit=function) if equal else set()


def _prove_versions(
    project: Project, host: Host, view: Project, function: str, file: Path, versions: Sequence[str]
) -> set[Path]:
    """Build every version at once in the pool; the first version in order that differs refuses."""
    from unbake import pool

    results = pool.run(host, _builds_row, [(project, view, host, function, file, version) for version in versions])
    for version, (equal, _) in zip(versions, results, strict=True):
        if not equal:
            raise Held(
                cause_named(
                    "land.mismatch",
                    (
                        f"land.mismatch: {function} compares exact but the {version} ROM built "
                        f"with it differs. The tree changed since the compare. Run: unbake compare "
                        f"{project.work / function / f'{function}.c'}"
                    ),
                    owner="land",
                    stage="land",
                )
            )
    return set().union(*(paths for _, paths in results))


@dataclass(frozen=True)
class Proof:
    versions: list[str]
    dependencies: set[Path]
    scores: dict[str, dict[str, Any]] | None = None
    dependency_headers: dict[str, str] = field(default_factory=dict)
    proposed_source: str | None = None


def prove(
    project: Project,
    host: Host,
    function: str,
    source: str,
    headers: dict[str, str],
    stage: Path,
    *,
    versions: tuple[str, ...] | None = None,
    source_edits: tuple[split.Edit, ...] = (),
) -> Proof:
    """Prove folded source against staged headers: exact ROM bytes, or admitted C in every holding version."""
    from unbake.layout import header_step
    from unbake.project import header_dependencies

    closed = header_dependencies.complete(project, source, headers)
    dependency_headers = {name: text for name, text in closed.headers.items() if headers.get(name) != text}
    headers, source = closed.headers, closed.source
    include = stage / "include"
    for name, text in headers.items():
        atomic_files.text(include / name, text)
    # A staged header's quoted includes ("../types.h") resolve beside it, so the rest of the tree is linked in.
    root = project.include[-1]
    for path in root.rglob("*"):
        mirror = include / path.relative_to(root)
        if path.is_file() and not mirror.exists():
            mirror.parent.mkdir(parents=True, exist_ok=True)
            atomic_files.copyfile(path, mirror)
    file = stage / "src" / f"{function}.c"
    atomic_files.text(file, source)
    view = replace(project, work_include=(include,))
    versions = split.holding_versions(project, function) if versions is None else versions
    scores = None
    dependencies = _prove_versions(project, host, view, function, file, versions)
    publish_inputs = set()
    from unbake.compilers.registry import compiler_directory, specification

    native = tuple(compiler_directory(project.tools, specification(ident)) for ident in project.compilers)
    for dependency in dependencies:
        path = root / dependency.relative_to(include) if dependency.is_relative_to(include) else dependency
        if not path.is_relative_to(project.root) or any(path.is_relative_to(home) for home in native):
            continue
        if any(path.is_relative_to(home) for home in (project.build, project.cache, project.roms)):
            raise Held(
                cause_named(
                    "land.dependency",
                    f"land.dependency: {path}: published source depends on disposable or ROM input",
                    owner="land",
                    stage="land",
                )
            )
        if path.resolve() != path:
            raise Held(
                cause_named(
                    "land.dependency",
                    f"land.dependency: {path}: required regular project input, not a symlink",
                    owner="land",
                    stage="land",
                )
            )
        # New staged headers are installed by this same publication.
        if not path.is_file() and (not path.is_relative_to(root) or path.relative_to(root).as_posix() not in headers):
            raise Held(
                cause_named(
                    "land.dependency",
                    f"land.dependency: {path}: required regular project input",
                    owner="land",
                    stage="land",
                )
            )
        publish_inputs.add(path)
    changed = {
        project.include[-1] / name: text.encode()
        for name, text in headers.items()
        if not (project.include[-1] / name).is_file() or (project.include[-1] / name).read_text() != text
    }
    for edit in source_edits:
        if not edit.path.is_file() or edit.path.read_text() != edit.before:
            raise Held(
                cause_named(
                    "land.consumer",
                    f"land.consumer: {edit.path}: source changed since fold",
                    owner="land",
                    stage="land",
                )
            )
        changed[edit.path] = edit.after.encode()
    if changed:
        from unbake.layout import redeclarations

        # An inferred own entry can be replaced by the exact implementation.
        # A compile-only check cannot detect changed argument extension or a
        # consumer's incompatible result register. Reprove affected published
        # native bodies whenever a header's function contract changes.
        entry_changed = any(
            redeclarations.catalog(data.decode()).get(function)
            != redeclarations.catalog(path.read_text() if path.is_file() else "").get(function)
            for path, data in changed.items()
            if path.suffix == ".h"
        )
        # A published unit's own source is validated as its new text, the one this land writes.
        if source_edits:
            header_step.validate(project, host, changed, prove_all=True, preproved=frozenset({function}))
        else:
            header_step.validate(
                project,
                host,
                {**changed, project.src / f"{function}.c": source.encode()},
                prove_all=entry_changed,
                preproved=frozenset({function}) if entry_changed else frozenset(),
            )
    return Proof(list(versions), publish_inputs, scores, dependency_headers, source)


def _row_edits(project: Project, function: str, versions: list[str], kind: str = "c") -> list[split.Edit]:
    """asm -> KIND (c, or hasm for original asm) for F's row in every holding version, path = F."""
    edits = []
    for version in versions:
        row = compare.row_of(project, function, version)
        path = project.version(version).split
        text, lines, segments = split.layout(path)
        lines = list(lines)
        for segment in segments:
            for candidate in segment.rows:
                if candidate.start == row.start and candidate.kind in ("asm", "c", "hasm"):
                    lines[candidate.line] = split.replace_row(
                        lines[candidate.line], candidate.match, kind=kind, path=function
                    )
        after = "".join(lines)
        if after != text:
            edits.append(split.Edit(path, text, after, (version,)))
    return edits


def subject(project: Project, function: str) -> str:
    """The land commit subject, read before the write: "Clean F" for a published unit, else "Match F"."""
    return f"{'Clean' if compare.published(project, function) else 'Match'} {function}"


def _refuse_edited_headers(project: Project) -> None:
    """Publish places declarations in the shared headers; a hand-edited generated header is dropped by the next
    regeneration, so it is refused before any work."""
    from unbake.project import generated_state

    if edited := generated_state.reconcile(project, "headers"):
        raise Held(
            cause_named(
                "land.generated_edit",
                (
                    f"land.generated_edit: generated headers were edited by hand: "
                    f"{', '.join(edited)}. Put the declarations in the draft; publish places "
                    f"them in the shared headers"
                ),
                owner="land",
                stage="land",
            )
        )


def publication_inputs(project: Project, function: str, versions: Sequence[str], dependencies: set[Path]) -> set[Path]:
    """Persistent producing closure, independent of which bytes this land wrote."""
    from unbake.compilers import drivers
    from unbake.project.headers import Graph

    source = project.src / (function + ".c")
    graph = Graph.capture(project)
    files = {source, project.root / "config.toml", project.root / "layout.toml", *dependencies}
    files.update(
        project.root / name
        for name in (
            "Makefile",
            "units.mk",
            "tools/compilers.sha256",
            "tools/n64link.version",
            "tools/compiler-driver.sha256",
            "tools/compiler_contracts.py",
            "tools/recipe_options.py",
        )
    )
    for version in versions:
        closure = graph.closure((source,), drivers.flags(project, version, function))
        if closure.unknown:
            raise Held(
                cause_named(
                    "land.dependencies",
                    "persistent include closure is unknown" + "".join(f"\n  {u}" for u in closure.unresolved),
                    owner="land",
                    stage="publication",
                )
            )
        files.update(closure.paths)
        files.update((project.version(version).split, project.version(version).symbols))
        files.update(
            project.root / "versions" / version / name
            for name in ("slices.mk", "symbols.ld", project.name + ".ld", project.name + ".data.ld")
        )
    if any(not path.is_relative_to(project.root) or not path.is_file() or path.is_symlink() for path in files):
        raise Held(
            cause_named(
                "land.dependencies",
                "persistent producing inputs must be regular project files",
                owner="land",
                stage="publication",
            )
        )
    return files


def _receipt(
    project: Project, host: Host, function: str, source: Path, versions: Sequence[str], dependencies: set[Path]
) -> dict[str, Any]:
    """Read back the already proved publication inputs; no second native proof or parallel store."""

    files = publication_inputs(project, function, versions, dependencies)
    return {
        "versions": list(versions),
        "files": {
            path.relative_to(project.root).as_posix(): inputs.digest(
                path, algorithm="sha256", reuse=retention.configured()
            )
            for path in sorted(files)
        },
        "dependency_manifest": [
            {
                "path": path.relative_to(project.root).as_posix(),
                "digest": inputs.digest(path, algorithm="sha256", reuse=False),
                "role": "source" if path == source else "build_input",
                "generated_from": ["config.toml", "layout.toml"]
                if path.name in ("Makefile", "units.mk", "slices.mk", "symbols.ld")
                else [],
            }
            for path in sorted(files)
        ],
        "configured_rom_sha1": {v: project.version(v).baserom_sha1 for v in versions},
        "compiler": project.compiler_reference(function),
        "host_inputs": {
            str(path): inputs.digest(path, algorithm="sha256", reuse=retention.configured()) for path in host.sources
        },
        "host_values": host.values,
    }


@journal.transactional
def land(
    project: Project,
    host: Host,
    file: Path,
    *,
    required_versions: tuple[str, ...] | None = None,
    on_commit: Callable[[dict[str, Any]], None] | None = None,
) -> str:
    """Land one draft; nothing is written until all required, already published and selected freebie versions prove."""
    from unbake.decomp import checks
    from unbake.fold import apply as fold_apply
    from unbake.layout import map as layout_map
    from unbake.report import progress

    _refuse_edited_headers(project)
    function = compare.function_of(file)
    previous_fuzzy = attempts.ledger(project).fuzzy(function)
    text = file.read_text()
    proposed_source_sha256 = hashlib.sha256(text.encode()).hexdigest()
    if previous_fuzzy is not None and file.resolve() == (project.src / f"{function}.c").resolve():
        text = attempts.unguarded(text)
    attempt = exact_attempt(project, host, function, file, required_versions=required_versions)
    owning_path = project.src / f"{function}.c"
    message = subject(project, function)
    selected = (
        None
        if required_versions is None or attempt is None
        else publication_versions(project, function, attempt, required_versions)
    )
    ident = (attempt.compiler if attempt is not None else "") or project.compiler_reference(function)
    config_path = project.root / "config.toml"
    config_before = config_path.read_bytes()
    config_staged_before = _git(project, "show", ":config.toml").encode()
    project = _attempt_recipe(project, function, attempt)
    # The writer admits new measured split rows before fold's strict ownership read.
    # Admission checks above still refuse invalid requests without changing the map.
    with tui.task("Indexing layout"):
        layout_map.ensure(project)
    with tui.task("Folding shared declarations"):
        folded = fold_apply.fold(
            project,
            host,
            function,
            text,
            versions=selected,
            exact_entry=attempt,
        )
    owning_source = folded.contract.previous_source if folded.contract is not None else None
    owning_providers = (
        {
            name: (project.include[-1] / name).read_bytes() if (project.include[-1] / name).is_file() else None
            for name in folded.headers
        }
        if folded.contract is not None
        else {}
    )
    result = checks.findings(project, (file,), Cache(project.cache), proposed={file: folded.source})
    broken = [row.finding for row in (result.unmarked)]
    if broken:
        rule_key = "land.rules"
        raise Held(
            cause_named(
                "land.land",
                f"{rule_key}: {function}: " + "; ".join(checks.plain(row) for row in broken),
                owner="land",
                stage="land",
            )
        )
    source = folded.source
    headers = {**fold_apply.private_headers(project, function), **folded.headers}
    project.work.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=".land-" + function + "-", dir=project.work))
    try:
        options: dict[str, Any] = {}
        if folded.source_edits:
            options["source_edits"] = folded.source_edits
        with tui.task("Proving every version"):
            proof = prove(project, host, function, source, headers, stage, versions=selected, **options)
        versions, dependencies = proof.versions, proof.dependencies
        headers.update(proof.dependency_headers)
        source = proof.proposed_source if proof.proposed_source is not None else source
    finally:
        shutil.rmtree(stage, ignore_errors=True)
    config_content = _compiler_config(project, function, ident, config_before, staged_before=config_staged_before)
    config_local = _unit_config(project, function, ident, config_before)
    config_staged = _unit_config(project, function, ident, config_staged_before)
    # A private work directory can retain declarations from an abandoned draft.
    # Only native prerequisites follow the source; explicit shared fold edits
    # still belong to their separately validated consumers.
    headers = {
        name: text
        for name, text in headers.items()
        if name in folded.headers or project.include[-1] / name in dependencies
    }
    from unbake.layout import header_loss

    with tui.task("Checking header loss"):
        header_loss.check(
            project,
            {
                project.src / f"{function}.c": source.encode(),
                **{project.include[-1] / n: t.encode() for n, t in headers.items()},
                **{edit.path: edit.after.encode() for edit in folded.source_edits},
            },
            policy=host,
        )
    written = {
        project.src / f"{function}.c",
        config_path,
        *(edit.path for edit in folded.source_edits),
        *(Path(edit.path) for edit in ([*folded.split_edits, *_row_edits(project, function, versions)])),
        *(
            project.include[-1] / name
            for name, text in headers.items()
            if not (project.include[-1] / name).is_file() or (project.include[-1] / name).read_text() != text
        ),
    }
    if owning_source is not None and (not owning_path.is_file() or owning_path.read_text() != owning_source):
        raise Held(
            cause_named(
                "land.own_contract",
                f"land.own_contract: {function}: owning source changed since proof",
                owner="land",
                stage="land",
            )
        )
    if folded.contract is not None and owning_source is None and owning_path.exists():
        raise Held(
            cause_named(
                "land.own_contract",
                f"land.own_contract: {function}: a new owning source appeared since proof",
                owner="land",
                stage="land",
            )
        )
    for name, provider_before in owning_providers.items():
        path = project.include[-1] / name
        provider_current = path.read_bytes() if path.is_file() else None
        if provider_current != provider_before:
            raise Held(
                cause_named(
                    "land.own_contract",
                    f"land.own_contract: {function}: provider {name} changed since proof",
                    owner="land",
                    stage="land",
                )
            )
    for edit in folded.source_edits:
        if not edit.path.is_file() or edit.path.read_text() != edit.before:
            raise Held(
                cause_named(
                    "land.consumer",
                    f"land.consumer: {edit.path}: source changed since proof",
                    owner="land",
                    stage="land",
                )
            )
    atomic_files.write(project.src / f"{function}.c", source.encode())
    for edit in folded.source_edits:
        atomic_files.write(edit.path, edit.after.encode())
    for name, text in headers.items():
        target = project.include[-1] / name
        if not target.is_file() or target.read_text() != text:
            atomic_files.write(target, text.encode())
    for edit in [*folded.split_edits, *_row_edits(project, function, versions)]:
        current = Path(edit.path).read_text() if Path(edit.path).is_file() else ""
        atomic_files.write(Path(edit.path), (edit.after if current == edit.before else current).encode())
    # Generate and commit from the scoped config; unrelated staged unit options stay local.
    atomic_files.write(config_path, config_content)
    from unbake import config

    updated = config.load(project.root)
    from unbake.report import state

    receipts = state.inventory(updated).receipts
    receipts.pop(function, None)
    generated = buildfiles.write(updated, host)
    units_path = project.root / "units.mk"
    if units_path.is_file():
        generated.append(units_path)
    steps.record(updated, "buildfiles", buildfiles.input_key(updated, host))
    generated += progress.write(updated, host, receipts=receipts)
    steps.record(updated, "progress", steps.STEPS["progress"].key(updated, host))
    attempts.ledger(updated).record_publication(
        function,
        None,
        source,
        versions,
        ident,
        compare.operation_dependencies(updated, host, updated.src / f"{function}.c"),
        committed=False,
    )
    dependencies.update(publication_inputs(updated, function, versions, dependencies))
    receipt = _receipt(updated, host, function, updated.src / f"{function}.c", versions, dependencies)
    with tui.task("Committing"):
        _commit(project, host, sorted({*written, *generated, *dependencies, project.root / attempts.PATH}), message)
    for entry in receipt["dependency_manifest"]:
        committed = _git(project, "show", "HEAD:" + entry["path"]).encode()
        if hashlib.sha256(committed).hexdigest() != entry["digest"]:
            raise Held(
                cause_named(
                    "land.committed_closure",
                    "committed producing input differs: " + entry["path"],
                    owner="land",
                    stage="publication",
                )
            )
    _restore_compiler_config(project, host, config_local, config_staged)
    commit = _git(project, "rev-parse", "HEAD").strip()
    if on_commit is not None:
        on_commit(
            {
                "function": function,
                "commit": commit,
                "message": message,
                "proof": {
                    **receipt,
                    **({"compared_sha256": attempt.sha256} if attempt is not None else {}),
                    **(
                        {
                            "own_contract": {
                                "previous_source_sha256": hashlib.sha256(owning_source.encode()).hexdigest()
                                if owning_source is not None
                                else None,
                                "previous_provider_sha256": {
                                    name: hashlib.sha256(before).hexdigest() if before is not None else None
                                    for name, before in owning_providers.items()
                                },
                                "proposed_source_sha256": proposed_source_sha256,
                                "published_source_sha256": hashlib.sha256(source.encode()).hexdigest(),
                                "consumer_sources": [
                                    {
                                        "path": edit.path.relative_to(project.root).as_posix(),
                                        "previous_source_sha256": hashlib.sha256(edit.before.encode()).hexdigest(),
                                        "published_source_sha256": hashlib.sha256(edit.after.encode()).hexdigest(),
                                    }
                                    for edit in folded.source_edits
                                ],
                            }
                        }
                        if folded.contract is not None
                        else {}
                    ),
                },
            }
        )
    shutil.rmtree(project.work / function, ignore_errors=True)
    if headers:
        steps.acknowledge_outputs(project, "headers", [project.include[-1] / name for name in headers])
    return commit


@journal.transactional
def land_original(
    project: Project, host: Host, function: str, *, on_commit: Callable[[dict[str, Any]], None] | None = None
) -> str:
    """Land one original-asm function as src/F.s; return the commit id. Every holding VERSION must prove the same
    rule from its ROM bytes and give the same .s text, and that text must assemble and link to each ROM row."""
    from unbake import config
    from unbake.decomp import exclusions, original_asm
    from unbake.report import progress

    _refuse_edited_headers(project)
    versions = list(split.holding_versions(project, function))
    rows = [compare.row_of(project, function, version) for version in versions]
    if any(row.kind != "asm" for row in rows):
        raise Held(
            cause_named(
                "land.original_kind",
                f"land.original_kind: {function}: only an unlanded asm row lands as original asm",
                owner="land",
                stage="land",
            )
        )
    bodies = [split.words(project, row) for row in rows]
    proofs = [original_asm.prove(project, row, data) for row, data in zip(rows, bodies, strict=True)]
    if len({found.rule for found in proofs}) != 1:
        raise Held(
            cause_named(
                "land.original_versions",
                f"land.original_versions: {function}: versions prove different rules",
                owner="land",
                stage="land",
            )
        )
    found = proofs[0]
    texts = {original_asm.write_source(project, host, row, data, found) for row, data in zip(rows, bodies, strict=True)}
    if len(texts) != 1:
        raise Held(
            cause_named(
                "land.original_versions",
                f"land.original_versions: {function}: versions need different .s text",
                owner="land",
                stage="land",
            )
        )
    text = texts.pop()
    records = original_asm.load(project)
    records[function] = original_asm.Record(found.rule)
    written = {
        project.src / f"{function}.s",
        project.root / original_asm.MANIFEST,
        *(
            Path(edit.path)
            for edit in [
                *exclusions.publication_edit(project, {function}),
                *_row_edits(project, function, versions, "hasm"),
            ]
        ),
    }
    atomic_files.write(project.src / f"{function}.s", text.encode())
    atomic_files.write(project.root / original_asm.MANIFEST, original_asm.dumps(records).encode())
    for edit in [
        *exclusions.publication_edit(project, {function}),
        *_row_edits(project, function, versions, "hasm"),
    ]:
        current = Path(edit.path).read_text() if Path(edit.path).is_file() else ""
        atomic_files.write(Path(edit.path), (edit.after if current == edit.before else current).encode())
    updated = config.load(project.root)
    generated = buildfiles.write(updated, host)
    steps.record(updated, "buildfiles", buildfiles.input_key(updated, host))
    generated += progress.write(updated, host)
    steps.record(updated, "progress", steps.STEPS["progress"].key(updated, host))
    attempts.ledger(updated).record_publication(
        function,
        None,
        text,
        versions,
        updated.compiler_reference(function),
        compare.operation_dependencies(updated, host, updated.src / f"{function}.s"),
        committed=False,
        kind="original",
    )
    _commit(project, host, sorted({*written, *generated}), f"Original asm {function}")
    commit = _git(project, "rev-parse", "HEAD").strip()
    if on_commit is not None:
        on_commit(
            {
                "function": function,
                "commit": commit,
                "message": f"Original asm {function}",
                "proof": {
                    **_receipt(
                        updated,
                        host,
                        function,
                        updated.src / f"{function}.s",
                        versions,
                        {updated.root / original_asm.MANIFEST},
                    ),
                    "original_rule": found.rule,
                },
            }
        )
    return commit


def publish(
    project: Project,
    host: Host,
    files: list[Path],
    *,
    originals: tuple[str, ...] = (),
    required_versions: tuple[str, ...] | None = None,
    on_commit: Callable[[dict[str, Any]], None] | None = None,
    compare_first: bool = False,
) -> Landed:
    """`unbake publish FILE... [--original NAME...]`: land each draft file, then each original-asm function, in
    turn; one failure does not stop the others."""
    from unbake import config

    result = Landed()
    if originals and required_versions is not None:
        raise Held(
            cause_named(
                "publish.versions",
                "publish.versions: --original requires proof in every holding version",
                owner="land",
                stage="publish",
            )
        )

    if files or originals:
        from unbake.typemap import mapping

        mapping.validate_contract(project)

    committed_records: dict[str, dict[str, Any]] = {}

    def committed(record: dict[str, Any]) -> None:
        journal.accepted()
        committed_records[record["function"]] = {
            "commit": record["commit"],
            "proof": {"versions": record["proof"]["versions"]},
        }
        if on_commit is not None:
            on_commit(record)

    callback = committed

    def draft(file: Path) -> Callable[[Project], str]:
        options: dict[str, Any] = {}
        return lambda current: land(
            current, host, file, required_versions=required_versions, on_commit=callback, **options
        )

    def original(name: str) -> Callable[[Project], str]:
        return lambda current: land_original(current, host, name, on_commit=callback)

    work = [(file.stem, draft(file)) for file in files] + [(name, original(name)) for name in originals]
    for position, (name, action) in enumerate(work):
        committed_records.clear()
        current = config.load(project.root)
        try:
            with journal.transaction(current):
                if compare_first and position < len(files):
                    compare.compare(current, host, files[position], required_versions=required_versions)
                commit = action(current)
        except KeyboardInterrupt:
            result.interrupted = True
            record = committed_records.get(name)
            if record is not None:
                result.landed.append(name)
                result.commits.append(record["commit"])
                result.versions[name] = list(record["proof"]["versions"])
            result.ready = [item for item, _ in work[position + (record is not None) :]]
            break
        except Held as error:
            result.failed[name] = {
                **error.data,
                "key": error.key,
                "reason": error.reason,
                "fault": capture(
                    error, cause=cause_named("land.unexpected", str(error), owner="land", stage="land")
                ).document(),
            }
            continue
        result.landed.append(name)
        result.commits.append(commit)
        updated = config.load(project.root)
        result.versions[name] = [
            v for v in split.holding_versions(updated, name) if compare.row_of(updated, name, v).kind in ("c", "hasm")
        ]
        try:
            with journal.transaction(config.load(project.root)):
                steps.ensure(config.load(project.root), host, ["merge-units"])
        except KeyboardInterrupt:
            result.interrupted = True
            result.ready = [item for item, _ in work[position + 1 :]]
            break
        except Held as error:
            # The accepted commit and its immediate event are durable even
            # when subsequent tree maintenance refuses. Do not relabel it as
            # an unlanded source or discard the successful prefix's receipt.
            result.post_commit_failure = {
                "function": name,
                "commit": commit,
                "key": error.key,
                "reason": error.reason,
                "fault": capture(
                    error, cause=cause_named("land.unexpected", str(error), owner="land", stage="land")
                ).document(),
            }
            break
    return result
