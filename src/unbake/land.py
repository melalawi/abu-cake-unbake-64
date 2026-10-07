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
import re
import shutil
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import toml  # type: ignore[import-untyped]

from unbake import atomic as atomic_files
from unbake import buildfiles, process, runner, scratch, steps
from unbake import cache as retention
from unbake.cache import Cache
from unbake.config import Held, Host, Project
from unbake.layout import split
from unbake.process import Fault, capture
from unbake.process import named as cause_named
from unbake.project import publication_transaction
from unbake.project.headers import scan
from unbake.work import attempts, compare


@dataclass
class Landed:
    landed: list[str] = field(default_factory=list)
    commits: list[str] = field(default_factory=list)
    failed: dict[str, dict[str, Any]] = field(default_factory=dict)
    versions: dict[str, list[str]] = field(default_factory=dict)
    post_commit_failure: dict[str, Any] | None = None
    fuzzy: dict[str, dict[str, Any]] = field(default_factory=dict)
    interrupted: bool = False
    ready: list[str] = field(default_factory=list)

    def document(self) -> dict[str, Any]:
        return {
            "landed": self.landed,
            "commits": self.commits,
            "failed": self.failed,
            "versions": self.versions,
            **({"post_commit_failure": self.post_commit_failure} if self.post_commit_failure is not None else {}),
            **({"fuzzy": self.fuzzy} if self.fuzzy else {}),
            **({"ready": self.ready, "retryable": True} if self.interrupted else {}),
        }

    def lines(self) -> list[str]:
        out = [
            f"{'fuzzy source' if name in self.fuzzy else 'landed'} {name} ({commit[:12]})"
            for name, commit in zip(self.landed, self.commits, strict=True)
        ]
        out += [f"not landed {name}: {failure['reason']}" for name, failure in self.failed.items()]
        if self.post_commit_failure is not None:
            out.append(f"after commit {self.post_commit_failure['commit']}: {self.post_commit_failure['reason']}")
        return out


def _git(project: Project, *args: str, env: dict[str, str] | None = None) -> str:
    return process.run_native(["git", *args], project.root, "land", temporary_root=project.build, env=env).stdout


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
    names = [str(path.relative_to(project.root)) for path in paths]
    _refuse_dangling_includes(project, names)
    index = Path(_git(project, "rev-parse", "--git-path", "index").strip())
    if not index.is_absolute():
        index = project.root / index
    before = index.read_bytes() if index.is_file() else None
    author = f"{host.publish_author_name} <{host.publish_author_email}>"
    try:
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
            message,
            "--author",
            author,
            "--only",
            "--",
            *names,
        )
    except BaseException:
        if before is None:
            index.unlink(missing_ok=True)
        else:
            atomic_files.write(index, before)
        raise
    publication_transaction.accepted()


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
    exact = {v for v in holding if attempt.versions.get(v, {}).get("exact") is True}
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


def exact_attempt(
    project: Project, function: str, file: Path, *, required_versions: tuple[str, ...] | None = None
) -> attempts.Attempt:
    """The newest compare of FILE's current bytes, which must be exact; refuse in words that say what to do."""
    from unbake.decomp import checks

    digest = hashlib.sha256(file.read_bytes()).hexdigest()
    found = [row for row in attempts.ledger(project).history(function) if row.sha256 == digest]
    if not found:
        raise Held(
            cause_named(
                "land.not_compared",
                f"land.not_compared: {function} was not compared since its last edit. Run: unbake compare {file}",
                owner="land",
                stage="land",
            )
        )
    attempt = found[-1]
    if required_versions is not None:
        publication_versions(project, function, attempt, required_versions)
        broken = [row.finding for row in checks.findings(project, (file,), Cache(project.cache)).unmarked]
        if broken:
            raise Held(
                cause_named(
                    "land.exact_attempt",
                    f"land.rules: {function}: " + "; ".join(checks.plain(f) for f in broken),
                    owner="land",
                    stage="land",
                )
            )
        return attempt
    comparison_refusal(function, attempt, tuple(attempt.versions))
    if attempt.exact:
        broken = [row.finding for row in checks.findings(project, (file,), Cache(project.cache)).unmarked]
        if broken:
            raise Held(
                cause_named(
                    "land.exact_attempt",
                    f"land.rules: {function}: " + "; ".join(checks.plain(f) for f in broken),
                    owner="land",
                    stage="land",
                )
            )
        return attempt
    version, row = min(attempt.versions.items(), key=lambda item: item[1]["percent"])
    if row["percent"] < 100:
        raise Held(
            cause_named(
                "land.not_exact",
                (
                    f"land.not_exact: {function} matches only {attempt.best_percent:.2f}% "
                    f"(lowest version {version}). Publish needs 100% in every version"
                ),
                owner="land",
                stage="land",
            )
        )
    lines = [checks.plain(row.finding) for row in checks.findings(project, (file,), Cache(project.cache)).unmarked]
    raise Held(
        cause_named(
            "land.rules",
            f"land.rules: {function} matches 100% but breaks the source rules: {'; '.join(lines)}",
            owner="land",
            stage="land",
        )
    )


def _with_compiler(project: Project, function: str, ident: str) -> Project:
    units = {name: value for name, value in project.units.items() if name != function}
    if ident != project.default_compiler or project.unit_flags.get(function):
        units[function] = ident
    return replace(project, units=units)


def _compiler_config(project: Project, function: str, ident: str, before: bytes) -> bytes:
    """Keep the proved unit options and refuse config edits outside this publication's scope."""
    path = project.root / "config.toml"
    if path.read_bytes() != before:
        raise Held(
            cause_named("land.config", "land.config: config.toml changed since proof", owner="land", stage="land")
        )
    data = toml.loads(before.decode())
    if "config.toml" in dirty(project):
        committed = toml.loads(_git(project, "show", "HEAD:config.toml"))

        def other_options(document: dict[str, Any]) -> dict[str, Any]:
            units = {name: row for name, row in document.get("units", {}).items() if name != function}
            return {**{key: value for key, value in document.items() if key != "units"}, "units": units}

        if other_options(data) != other_options(committed):
            raise Held(
                cause_named(
                    "land.config",
                    f"land.config: {function}: unproved config changes outside its unit options",
                    owner="land",
                    stage="land",
                )
            )
    units = dict(data.get("units", {}))
    flags = project.unit_flags.get(function, ())
    if ident != project.default_compiler or flags:
        units[function] = {"compiler": ident, **({"flags": list(flags)} if flags else {})}
    else:
        units.pop(function, None)
    if units == data.get("units", {}):
        return before
    if units:
        data["units"] = dict(sorted(units.items()))
    else:
        data.pop("units", None)
    text: str = toml.dumps(data)
    return text.encode()


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
        linked = runner.link(project, host, placed, version, row, work, file)
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


def _fuzzy_signature(project: Project, function: str, version: str, source: str) -> None:
    """A nonmatching body cannot use byte equality to excuse an invented entry ABI."""
    from unbake.compilers.families import family_for
    from unbake.decomp.draft_abi import declared_void_exit, leaf_entry_record, mapped_body
    from unbake.layout import redeclarations
    from unbake.typemap import declarations, header_names, o32, types_db

    body = mapped_body(project, function, version)
    if body is None:
        raise Held(
            cause_named(
                "land.fuzzy_identity",
                f"land.fuzzy_identity: {function} VERSION {version}: no current mapped entry",
                owner="land",
                stage="land",
            )
        )
    database = types_db.path(project)
    record = types_db.entries(database, "functions", [function]).get(function, {})
    record = leaf_entry_record(record, function, version, body)
    abi = record.get("abi", {})
    if abi.get("return_width") == 8 and (not abi.get("return_pair_known") or abi.get("conflicts")):
        raise Held(
            cause_named(
                "land.fuzzy_abi",
                f"land.fuzzy_abi: {function}: consumed integer return pair is unproven or contradictory",
                owner="land",
                stage="land",
            )
        )
    expected = record.get("prototype") if record.get("state") == "known" else None
    declared_void = False
    if (
        expected is None
        and not abi.get("return_known")
        and record.get("return", {}).get("state") == "known"
        and record.get("return", {}).get("type") == "void"
        and isinstance(body, dict)
        and body.get("calls")
    ):
        last = max(body["calls"], key=lambda call: call["instruction"])
        name = last.get("callee")
        if name:
            callee = types_db.entries(database, "functions", [name]).get(name, {})
            declared_void = declared_void_exit(record, function, version, body, callee)
    if (
        expected is None
        and abi.get("arity_known")
        and (abi.get("return_known") or abi.get("discardable_return") or declared_void)
        and not abi.get("conflicts")
        and not abi.get("missing")
    ):
        expected = record.get("abi_declaration", {}).get("prototype")
    actual = declarations.extract(source, {"function": function, "version": version}, definitions=True)
    own = actual["functions"].get(function)
    aliases = {name: declarations.canonical(type_, {}) for name, type_ in actual["aliases"].items()}
    if (
        record.get("state") != "known"
        and own is not None
        and o32.admits(source, own, record.get("machine_signature", {}), aliases)
    ):
        _fuzzy_calls(function, source, intrinsics=family_for(project.compiler_for(function)).source_intrinsics())
        return
    if (
        expected
        and record.get("state") != "known"
        and abi.get("arity_known")
        and not abi.get("registers")
        and (abi.get("return_known") or abi.get("discardable_return"))
        and not abi.get("conflicts")
        and not abi.get("missing")
    ):
        # Incidental caller argument registers can keep a call carrier's C
        # list unspecified. The callee's own empty entry-read set still
        # proves an explicit no-argument definition's transport.
        expected = re.sub(r"(\b" + re.escape(function) + r"\s*)\(\s*\)", r"\g<1>(void)", expected)
    if not expected or re.search(r"\b" + re.escape(function) + r"\s*\(\s*\)", expected):
        raise Held(
            cause_named(
                "land.fuzzy_abi",
                f"land.fuzzy_abi: {function}: canonical entry signature is unresolved",
                owner="land",
                stage="land",
            )
        )
    expected = header_names.rewrite(expected, types_db.meta(database, "shared_aliases"), set())
    if own is None or not (
        redeclarations.equivalent(own["prototype"], expected, aliases)
        or o32.compatible_prototypes(own["prototype"], expected, aliases)
        or (
            record.get("state") != "known"
            and abi.get("arity_known")
            and (abi.get("return_known") or abi.get("discardable_return"))
            and not abi.get("conflicts")
            and not abi.get("missing")
            and o32.compatible_definition(source, function, own, expected, aliases)
        )
    ):
        raise Held(
            cause_named(
                "land.fuzzy_abi",
                f"land.fuzzy_abi: {function} VERSION {version}: definition differs from canonical `{expected}`",
                owner="land",
                stage="land",
            )
        )

    _fuzzy_calls(function, source, intrinsics=family_for(project.compiler_for(function)).source_intrinsics())


def _fuzzy_calls(function: str, source: str, *, intrinsics: tuple[str, ...] = ()) -> None:
    """Old compilers accept implicit function declarations; retained C must declare its calls."""
    from pycparser import c_ast, c_generator  # type: ignore[import-untyped]

    from unbake import cdecl

    try:
        tree = cdecl.parse(cdecl.declaration_source(source))
    except Exception as error:
        raise Held(
            capture(
                error,
                cause=cause_named(
                    "land.fuzzy_source",
                    f"land.fuzzy_source: {function}: cannot validate call declarations: {error}",
                    owner="land",
                    stage="land",
                ),
            )
        ) from error
    declared = {node.name for node in tree.ext if isinstance(node, (c_ast.Decl, c_ast.Typedef))}
    declared.update(node.decl.name for node in tree.ext if isinstance(node, c_ast.FuncDef))
    declared.update(intrinsics)
    definition = next(
        (node for node in tree.ext if isinstance(node, c_ast.FuncDef) and node.decl.name == function), None
    )
    if definition is None:
        raise Held(
            cause_named(
                "land.fuzzy_identity",
                f"land.fuzzy_identity: {function}: executable definition is missing",
                owner="land",
                stage="land",
            )
        )
    text = c_generator.CGenerator().visit(definition)
    if placeholder := next(
        (token[0] for token in cdecl.SOURCE_TOKEN.finditer(text) if re.fullmatch(r"M2C_\w+", token[0])), None
    ):
        raise Held(
            cause_named(
                "land.fuzzy_placeholder",
                f"land.fuzzy_placeholder: {function}: unresolved {placeholder}",
                owner="land",
                stage="land",
            )
        )
    if definition.decl.type.args is not None:
        declared.update(param.name for param in definition.decl.type.args.params if isinstance(param, c_ast.Decl))

    class Calls(c_ast.NodeVisitor):  # type: ignore[misc]
        def visit_Decl(self, node: Any) -> None:
            declared.add(node.name)
            if node.init is not None:
                self.visit(node.init)

        def visit_FuncCall(self, node: Any) -> None:
            if isinstance(node.name, c_ast.ID) and node.name.name not in declared:
                raise Held(
                    cause_named(
                        "land.fuzzy_undeclared",
                        f"land.fuzzy_undeclared: {function}: call to undeclared {node.name.name}",
                        owner="land",
                        stage="land",
                    )
                )
            self.generic_visit(node)

    Calls().visit(definition.body)


def _fuzzy_builds_row(spec: tuple[Project, Project, Host, str, Path, str]) -> tuple[dict[str, Any], set[Path]]:
    from unbake.compilers.fingerprint import _body
    from unbake.work.score import measure_words

    project, view, host, function, file, version = spec
    row = compare.row_of(project, function, version)
    try:
        with runner.compile_unit(
            view,
            host,
            file,
            version,
            unit=function,
            non_matching=True,
            verify_input=lambda text: _fuzzy_signature(project, function, version, text),
        ) as obj:
            _body(obj, function)  # the requested function must really be defined in executable code
            dependencies = runner.dependencies(view, host, file, version, unit=function, non_matching=True)
            try:
                linked, problems = runner.link_function(view, host, obj, version, row, file)
            except Held as error:
                # Compiling admitted C is sufficient for guarded retention. A
                # failed measurement remains unavailable, never a synthetic 0%.
                return {
                    "compiled": True,
                    "percent": None,
                    "exact": False,
                    "fault": capture(
                        error, cause=cause_named("land.unexpected", str(error), owner="land", stage="land")
                    ).document(),
                }, dependencies
            measured = measure_words(version, split.words(project, row), linked)
            assert measured.typed is not None
            measured.typed["relocation"] += len(problems)
            return {**measured.document(), "compiled": True, "problems": problems}, dependencies
    except (Held, ValueError) as error:
        return {
            "compiled": False,
            "percent": None,
            "exact": False,
            "reason": str(error),
            "fault": capture(
                error, cause=cause_named("land.unexpected", str(error), owner="land", stage="land")
            ).document(),
        }, set()


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
    fuzzy: bool = False,
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
            mirror.symlink_to(path)
    file = stage / "src" / f"{function}.c"
    atomic_files.text(file, source)
    view = replace(project, work_include=(include,))
    versions = split.holding_versions(project, function) if versions is None else versions
    scores = None
    if fuzzy:
        from unbake import pool

        results = pool.run(host, _fuzzy_builds_row, [(project, view, host, function, file, v) for v in versions])
        scores = dict(zip(versions, (row for row, _ in results), strict=True))
        failed = tuple({"version": v, **row} for v, row in scores.items() if not row["compiled"])
        if failed:
            raise Held(
                cause_named(
                    "land.fuzzy_compile",
                    f"land.fuzzy_compile: {function}: {len(failed)} holding versions refused",
                    owner="land",
                    stage="land",
                ),
                failures=failed,
            )
        dependencies = set().union(*(paths for _, paths in results))
    else:
        dependencies = _prove_versions(project, host, view, function, file, versions)
    publish_inputs = set()
    native = tuple(project.tools / ident for ident in project.compilers)
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
        if fuzzy or source_edits:
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


def _fuzzy_score(project: Project, function: str, scores: dict[str, dict[str, Any]]) -> float | None:
    if any(row.get("percent") is None for row in scores.values()):
        return None
    sizes = {v: compare.row_of(project, function, v).end - compare.row_of(project, function, v).start for v in scores}
    total = sum(sizes.values())
    if not total:
        raise Held(
            cause_named("land.fuzzy_score", f"land.fuzzy_score: {function}: empty target", owner="land", stage="land")
        )
    return sum(sizes[v] * float(scores[v]["percent"]) for v in scores) / total


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


def _receipt(
    project: Project, host: Host, function: str, source: Path, versions: Sequence[str], dependencies: set[Path]
) -> dict[str, Any]:
    """Read back the already proved publication inputs; no second native proof or parallel store."""
    from unbake import inputs

    files = {source, project.root / "config.toml", project.compiler_for(function).sha256, *dependencies}
    files.update(path for v in versions for path in (project.version(v).split, project.version(v).symbols))
    return {
        "versions": list(versions),
        "files": {
            path.relative_to(project.root).as_posix(): inputs.digest(
                path, algorithm="sha256", reuse=retention.configured()
            )
            for path in sorted(files)
        },
        "configured_rom_sha1": {v: project.version(v).baserom_sha1 for v in versions},
        "compiler": project.compiler_reference(function),
        "host_inputs": {
            str(path): inputs.digest(path, algorithm="sha256", reuse=retention.configured()) for path in host.sources
        },
        "host_values": host.values,
    }


def _own_contract_source(project: Project, function: str) -> str | None:
    """Pin actual old C ownership; a marked caller contract is not an old definition."""
    holding = split.holding_versions(project, function)
    path = project.src / f"{function}.c"
    if not holding:
        raise Held(
            cause_named(
                "land.own_contract",
                f"land.own_contract: {function}: current native owner is missing",
                owner="land",
                stage="land",
            )
        )
    if path.is_file():
        if any(compare.row_of(project, function, v).kind != "c" for v in holding):
            raise Held(
                cause_named(
                    "land.own_contract",
                    f"land.own_contract: {function}: an existing C owner must be exact in every holder",
                    owner="land",
                    stage="land",
                )
            )
        source = path.read_text()
        if source != _git(project, "show", f"HEAD:src/{function}.c"):
            raise Held(
                cause_named(
                    "land.own_contract",
                    f"land.own_contract: {function}: owning source differs from its committed identity",
                    owner="land",
                    stage="land",
                )
            )
        return source
    if any(compare.row_of(project, function, v).kind != "asm" for v in holding):
        raise Held(
            cause_named(
                "land.own_contract",
                f"land.own_contract: {function}: published owning source is missing",
                owner="land",
                stage="land",
            )
        )
    return None


def _compare_own_contract(project: Project, host: Host, file: Path) -> None:
    """Measure an explicit proposal in a private header view, never installed authority.

    No exact receipt is fabricated: compare creates the real complete attempt.
    Land later repeats the strict planner and target/current-consumer proofs in
    the existing transaction before it can install the proposed contract.
    """
    from unbake.fold import self_prototype, source_views
    from unbake.layout import index, redeclarations
    from unbake.layout.header_context import Headers

    function = compare.function_of(file)
    previous = _own_contract_source(project, function)
    view = compare.view_for(project, file, function)
    headers = Headers.read(view)
    manifest = set(index.load(project)["headers"])
    generated = frozenset(
        path
        for path in headers.texts
        if any(
            path.is_relative_to(root) and (path.relative_to(root).as_posix() in manifest or index.marked(path, root))
            for root in view.include
        )
    )
    edits = self_prototype.owned(
        headers.texts,
        generated,
        file.read_text(),
        function,
        redeclarations.aliases(list(headers.texts.values())),
        split.holding_versions(project, function),
        None,
        previous,
        self_prototype.measure_owned(project, function),
        provisional=True,
    )
    proposed = Headers({**headers.texts, **{edit.path: edit.after for edit in edits}}, root=headers.root)
    with scratch.temporary(host, project, "land", prefix="own-contract-compare-") as temporary:
        roots = source_views.header_includes(view, proposed, Path(temporary))
        compare.compare(replace(view, work_include=roots), host, file)


@publication_transaction.transactional
def land(
    project: Project,
    host: Host,
    file: Path,
    *,
    required_versions: tuple[str, ...] | None = None,
    on_commit: Callable[[dict[str, Any]], None] | None = None,
    fuzzy: bool = False,
    replace_own_contract: bool = False,
) -> str:
    """Land one draft; nothing is written until all required, already published and selected freebie versions prove."""
    from unbake.decomp import checks
    from unbake.fold import apply as fold_apply
    from unbake.layout import map as layout_map
    from unbake.report import progress

    _refuse_edited_headers(project)
    started = time.monotonic()
    function = compare.function_of(file)
    previous_fuzzy = attempts.ledger(project).fuzzy(function)
    text = file.read_text()
    proposed_source_sha256 = hashlib.sha256(text.encode()).hexdigest()
    if previous_fuzzy is not None and file.resolve() == (project.src / f"{function}.c").resolve():
        text = attempts.unguarded(text)
    if fuzzy:
        if required_versions is not None:
            raise Held(
                cause_named(
                    "land.fuzzy_versions",
                    "land.fuzzy_versions: fuzzy retention compiles every holding version",
                    owner="land",
                    stage="land",
                )
            )
        holding = split.holding_versions(project, function)
        if not holding or any(compare.row_of(project, function, v).kind != "asm" for v in holding):
            raise Held(
                cause_named(
                    "land.fuzzy_exact",
                    f"land.fuzzy_exact: {function}: fuzzy cannot replace exact C or original assembly",
                    owner="land",
                    stage="land",
                )
            )
        matching = [
            row
            for row in attempts.ledger(project).history(function)
            if row.sha256 == hashlib.sha256(file.read_bytes()).hexdigest()
        ]
        attempt = matching[-1] if matching else None
    else:
        attempt = exact_attempt(project, function, file, required_versions=required_versions)
    owning_source = None
    owning_evidence = None
    owning_path = project.src / f"{function}.c"
    if replace_own_contract:
        if fuzzy or required_versions is not None:
            raise Held(
                cause_named(
                    "land.own_contract",
                    "land.own_contract: replacement requires exact proof in every holding version",
                    owner="land",
                    stage="land",
                )
            )
        owning_source = _own_contract_source(project, function)
        from unbake.fold.self_prototype import measure_owned

        owning_evidence = measure_owned(project, function)
    message = f"Fuzzy {function}" if fuzzy else subject(project, function)
    selected = (
        None
        if required_versions is None or attempt is None
        else publication_versions(project, function, attempt, required_versions)
    )
    ident = (attempt.compiler if attempt is not None else "") or project.compiler_reference(function)
    config_path = project.root / "config.toml"
    config_before = config_path.read_bytes()
    project = _with_compiler(project, function, ident)
    # The writer admits new measured split rows before fold's strict ownership read.
    # Admission checks above still refuse invalid requests without changing the map.
    layout_map.ensure(project)
    owning_options: dict[str, Any] = (
        {"owning_source": owning_source, "owning_evidence": owning_evidence} if replace_own_contract else {}
    )
    folded = fold_apply.fold(
        project,
        host,
        function,
        text,
        versions=selected,
        exact_entry=None if fuzzy else attempt,
        **owning_options,
    )
    owning_providers = (
        {
            name: (project.include[-1] / name).read_bytes() if (project.include[-1] / name).is_file() else None
            for name in folded.headers
        }
        if replace_own_contract
        else {}
    )
    result = checks.findings(project, (file,), Cache(project.cache), proposed={file: folded.source})
    broken = [row.finding for row in (result.rows if fuzzy else result.unmarked)]
    if broken:
        rule_key = "land.fuzzy_rules" if fuzzy else "land.rules"
        raise Held(
            cause_named(
                "land.land",
                f"{rule_key}: {function}: " + "; ".join(checks.plain(row) for row in broken),
                owner="land",
                stage="land",
            )
        )
    if fuzzy and folded.split_edits:
        raise Held(
            cause_named(
                "land.fuzzy_identity",
                f"land.fuzzy_identity: {function}: resolve the proposed row ownership changes before retaining C",
                owner="land",
                stage="land",
            )
        )
    source = attempts.guarded(folded.source) if fuzzy else folded.source
    headers = {**fold_apply.private_headers(project, function), **folded.headers}
    stage = project.work / "_land" / function
    shutil.rmtree(stage, ignore_errors=True)
    try:
        options: dict[str, Any] = {"fuzzy": True} if fuzzy else {}
        if folded.source_edits:
            options["source_edits"] = folded.source_edits
        proof = prove(project, host, function, source, headers, stage, versions=selected, **options)
        versions, dependencies = proof.versions, proof.dependencies
        headers.update(proof.dependency_headers)
        source = proof.proposed_source if proof.proposed_source is not None else source
        fuzzy_receipt: dict[str, Any] | None = None
        if fuzzy:
            assert proof.scores is not None
            score = _fuzzy_score(project, function, proof.scores)
            if previous_fuzzy is not None:
                before = _git(project, "show", f"HEAD:src/{function}.c")
                if (
                    previous_fuzzy["source_sha256"] is not None
                    and hashlib.sha256(before.encode()).hexdigest() != previous_fuzzy["source_sha256"]
                ):
                    raise Held(
                        cause_named(
                            "land.fuzzy_history",
                            f"land.fuzzy_history: {function}: committed source differs from its receipt",
                            owner="land",
                            stage="land",
                        )
                    )
                old_score = previous_fuzzy["score"]
                if old_score is None:
                    baseline = prove(
                        _with_compiler(project, function, previous_fuzzy["compiler"]),
                        host,
                        function,
                        before,
                        {},
                        stage / "baseline",
                        fuzzy=True,
                    )
                    assert baseline.scores is not None
                    old_score = _fuzzy_score(project, function, baseline.scores)
                # Admission above requires a clean candidate. A tie may remove
                # violations from the committed bytes, never from a local edit.
                cleanup = score is not None and score == old_score and bool(checks.run(attempts.unguarded(before)))
                if score is None or old_score is None or score < old_score or (score == old_score and not cleanup):
                    raise Held(
                        cause_named(
                            "land.fuzzy_improvement",
                            (
                                f"land.fuzzy_improvement: {function}: a strictly higher measured score or "
                                f"an equal measured score with source-rule cleanup is required"
                            ),
                            owner="land",
                            stage="land",
                        )
                    )
            fuzzy_receipt = {
                "source_sha256": hashlib.sha256(source.encode()).hexdigest(),
                "compiler": ident,
                "score": score,
                "versions": {v: row["percent"] for v, row in proof.scores.items()},
            }
            attempts.ledger(project).compare(
                attempts.Attempt(
                    attempts.now(),
                    function,
                    fuzzy_receipt["source_sha256"],
                    compare.row_of(project, function, versions[0]).end
                    - compare.row_of(project, function, versions[0]).start,
                    proof.scores,
                    score,
                    False,
                    time.monotonic() - started,
                    ident,
                ),
                compare.operation_dependencies(project, host, file),
            )
    finally:
        shutil.rmtree(stage, ignore_errors=True)
    config_content = _compiler_config(project, function, ident, config_before)
    # A private work directory can retain declarations from an abandoned draft.
    # Only native prerequisites follow the source; explicit shared fold edits
    # still belong to their separately validated consumers.
    headers = {
        name: text
        for name, text in headers.items()
        if name in folded.headers or project.include[-1] / name in dependencies
    }
    from unbake.layout import header_loss

    header_loss.check(
        project,
        {
            project.src / f"{function}.c": source.encode(),
            **{project.include[-1] / n: t.encode() for n, t in headers.items()},
            **{edit.path: edit.after.encode() for edit in folded.source_edits},
        },
    )
    written: dict[Path, bytes | None] = {}

    def put(path: Path, content: bytes) -> None:
        if path not in written:
            written[path] = path.read_bytes() if path.is_file() else None
        atomic_files.write(path, content)

    try:
        if owning_source is not None and (not owning_path.is_file() or owning_path.read_text() != owning_source):
            raise Held(
                cause_named(
                    "land.own_contract",
                    f"land.own_contract: {function}: owning source changed since proof",
                    owner="land",
                    stage="land",
                )
            )
        if replace_own_contract and owning_source is None and owning_path.exists():
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
        put(project.src / f"{function}.c", source.encode())
        for edit in folded.source_edits:
            put(edit.path, edit.after.encode())
        for name, text in headers.items():
            target = project.include[-1] / name
            if not target.is_file() or target.read_text() != text:
                put(target, text.encode())
        for edit in [] if fuzzy else [*folded.split_edits, *_row_edits(project, function, versions)]:
            current = Path(edit.path).read_text() if Path(edit.path).is_file() else ""
            put(Path(edit.path), (edit.after if current == edit.before else current).encode())
        # Unchanged dirty options still supplied the proof and must travel with the source.
        put(config_path, config_content)
        from unbake import config

        if fuzzy or previous_fuzzy is not None:
            attempts.ledger(project).record_publication(
                function,
                fuzzy_receipt,
                source,
                versions,
                ident,
                compare.operation_dependencies(project, host, file),
                committed=False,
            )
        updated = config.load(project.root)
        receipts = attempts.ledger(updated).fuzzy_sources()
        if fuzzy_receipt is not None:
            receipts[function] = fuzzy_receipt
        else:
            receipts.pop(function, None)
        generated = buildfiles.write(updated, host, receipts=receipts)
        units_path = project.root / "units.mk"
        if units_path.is_file():
            generated.append(units_path)
        steps.record(updated, "buildfiles", buildfiles.input_key(updated, host))
        generated += progress.write(updated, host, receipts=receipts)
        steps.record(updated, "progress", steps.STEPS["progress"].key(updated, host))
        _commit(project, host, sorted({*written, *generated, *dependencies, project.root / attempts.PATH}), message)
    except BaseException:
        for path, previous in written.items():
            if previous is None:
                path.unlink(missing_ok=True)
            else:
                atomic_files.write(path, previous)
        raise
    commit = _git(project, "rev-parse", "HEAD").strip()
    attempts.ledger(updated).record_publication(
        function,
        fuzzy_receipt,
        source,
        versions,
        ident,
        compare.operation_dependencies(updated, host, updated.src / f"{function}.c"),
    )
    if on_commit is not None:
        on_commit(
            {
                "function": function,
                "commit": commit,
                "message": message,
                "proof": {
                    **_receipt(updated, host, function, updated.src / f"{function}.c", versions, dependencies),
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
                        if replace_own_contract
                        else {}
                    ),
                    **(
                        {
                            "kind": "fuzzy",
                            "score": fuzzy_receipt["score"],
                            "scores": proof.scores,
                            "default_rom": "original assembly rows retained",
                        }
                        if fuzzy_receipt is not None
                        else {}
                    ),
                },
            }
        )
    if not fuzzy:
        shutil.rmtree(project.work / function, ignore_errors=True)
    if headers:
        steps.acknowledge_outputs(project, "headers", [project.include[-1] / name for name in headers])
    return commit


@publication_transaction.transactional
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
    written: dict[Path, bytes | None] = {}

    def put(path: Path, content: bytes) -> None:
        if path not in written:
            written[path] = path.read_bytes() if path.is_file() else None
        atomic_files.write(path, content)

    try:
        put(project.src / f"{function}.s", text.encode())
        put(project.root / original_asm.MANIFEST, original_asm.dumps(records).encode())
        for edit in [
            *exclusions.publication_edit(project, {function}),
            *_row_edits(project, function, versions, "hasm"),
        ]:
            current = Path(edit.path).read_text() if Path(edit.path).is_file() else ""
            put(Path(edit.path), (edit.after if current == edit.before else current).encode())
        updated = config.load(project.root)
        generated = buildfiles.write(updated, host)
        steps.record(updated, "buildfiles", buildfiles.input_key(updated, host))
        generated += progress.write(updated, host)
        steps.record(updated, "progress", steps.STEPS["progress"].key(updated, host))
        _commit(project, host, sorted({*written, *generated}), f"Original asm {function}")
    except BaseException:
        for path, previous in written.items():
            if previous is None:
                path.unlink(missing_ok=True)
            else:
                atomic_files.write(path, previous)
        raise
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
    fuzzy: bool = False,
    compare_first: bool = False,
    replace_own_contract: bool = False,
) -> Landed:
    """`unbake publish FILE... [--original NAME...]`: land each draft file, then each original-asm function, in
    turn; one failure does not stop the others."""
    from unbake import config

    result = Landed()
    if replace_own_contract and (fuzzy or originals or required_versions is not None or len(files) != 1):
        raise Held(
            cause_named(
                "publish.own_contract",
                "publish.own_contract: name exactly one owning C FILE with exact all-holder proof",
                owner="land",
                stage="publish",
            )
        )
    if fuzzy and (originals or required_versions is not None):
        raise Held(
            cause_named(
                "publish.fuzzy",
                "publish.fuzzy: use C files without --original or --require-version",
                owner="land",
                stage="publish",
            )
        )
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
        publication_transaction.accepted()
        committed_records[record["function"]] = {
            "commit": record["commit"],
            "proof": {"versions": record["proof"]["versions"]},
        }
        if record.get("proof", {}).get("kind") == "fuzzy":
            result.fuzzy[record["function"]] = record["proof"]
        if on_commit is not None:
            on_commit(record)

    callback = committed

    def draft(file: Path) -> Callable[[Project], str]:
        options = {"fuzzy": True} if fuzzy else {}
        if replace_own_contract:
            options["replace_own_contract"] = True
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
            with publication_transaction.transaction(current):
                if compare_first and position < len(files):
                    if replace_own_contract:
                        _compare_own_contract(current, host, files[position])
                    else:
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
        result.versions[name] = (
            list(result.fuzzy[name]["versions"])
            if name in result.fuzzy
            else [
                v
                for v in split.holding_versions(updated, name)
                if compare.row_of(updated, name, v).kind in ("c", "hasm")
            ]
        )
        try:
            if not fuzzy:
                with publication_transaction.transaction(config.load(project.root)):
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
