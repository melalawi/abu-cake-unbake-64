"""Push a publication after reconciling the proofs reached by a concurrent update."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict
from pathlib import Path
from typing import Any

from unbake import atomic, config, journal, pool, process, runner, scratch, strict_json
from unbake.cache import Cache
from unbake.compilers import drivers
from unbake.config import Held, Host, Project
from unbake.layout import split
from unbake.process import capture
from unbake.process import named as cause_named
from unbake.work import attempts

ProofKey = tuple[str, str]
_INCLUDE = re.compile(r'^[ \t]*(?:#[ \t]*include|\.include)[ \t]*([<"])([^>"\n]+)[>"]', re.M)
_DIRECTIVE = re.compile(r"^[ \t]*#[ \t]*include\b[^\n]*", re.M)
_COMMENT = re.compile(r"/\*.*?\*/|//[^\n]*", re.S)


def _git(project: Project, *args: str) -> str:
    return process.run_tool(["git", *args], project.root, "publish").strip()


def snapshot(project: Project, host: Host) -> dict[ProofKey, str]:
    """One source/header read and one split scan per version; retain only compact pins.

    Literal imports deliberately overapproximate conditional branches. A computed
    import uses native dependency discovery for that unit/version. Shared headers
    are parsed once, and cycles cannot truncate another source's closure.
    """
    rows = {version: split.functions(project, version) for version in project.versions}
    fuzzy = attempts.ledger(project).fuzzy_sources()
    files: dict[Path, tuple[str, tuple[Path, ...], bool]] = {}

    def read(path: Path) -> tuple[str, tuple[Path, ...], bool]:
        path = path.resolve()
        if path in files:
            return files[path]
        if not path.is_file():
            files[path] = ("missing", (), False)
            return files[path]
        data = path.read_bytes()
        text = _COMMENT.sub("", data.decode(errors="replace"))
        edges = []
        for match in _INCLUDE.finditer(text):
            roots = (path.parent, *project.include) if match[1] == '"' else project.include
            choices = [(root / match[2]).resolve() for root in roots]
            edges.append(next((choice for choice in choices if choice.is_file()), choices[0]))
        computed = any(not _INCLUDE.match(match[0]) for match in _DIRECTIVE.finditer(text))
        files[path] = (hashlib.sha256(data).hexdigest(), tuple(edges), computed)
        return files[path]

    def closure(source: Path) -> tuple[dict[str, str], bool]:
        pins = {}
        pending = [source.resolve()]
        computed = False
        while pending:
            path = pending.pop()
            label = path.as_posix()
            if label in pins:
                continue
            digest, edges, dynamic = read(path)
            pins[label] = digest
            computed |= dynamic
            pending.extend(edges)
        return pins, computed

    scopes = {}
    closures: dict[Path, tuple[dict[str, str], bool]] = {}
    for version, members in rows.items():
        symbols = read(project.version(version).symbols)[0]
        for row in members:
            unit = Path(row.path).name
            if row.kind not in ("c", "hasm") and unit not in fuzzy:
                continue
            source = project.src / f"{unit}.{'s' if row.kind == 'hasm' else 'c'}"
            if source not in closures:
                closures[source] = closure(source)
            pins, computed = closures[source]
            pins = dict(pins)
            if computed and row.kind != "hasm":
                for path in runner.dependencies(project, host, source, version, unit=unit, non_matching=unit in fuzzy):
                    pins[str(path.resolve())] = read(path)[0]
            compiler = project.compiler_for(unit)
            values = {
                "files": pins,
                "row": asdict(row),
                "rom": project.version(version).baserom_sha1,
                "symbols": symbols,
                "compiler": compiler.id,
                "compiler_pin": read(compiler.sha256)[0],
                "flags": drivers.flags(project, version, unit, non_matching=unit in fuzzy),
                "cppflags": project.cppflags,
                "asflags": project.asflags,
                "gnu_asflags": project.gnu_asflags,
                "macros": project.version(version).macros,
                "host": host.values,
                "fuzzy": unit in fuzzy,
            }
            if row.kind == "hasm":
                from unbake.decomp import original_asm

                values["original_rule"] = read(project.root / original_asm.MANIFEST)[0]
            scopes[unit, version] = hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest()
    return scopes


def _prove(job: tuple[Project, Host, str, str]) -> dict[str, Any] | None:
    """Use the existing native publication gates on exactly one affected scope."""
    from unbake import land
    from unbake.decomp import original_asm
    from unbake.work import compare

    project, host, unit, version = job
    row = compare.row_of(project, unit, version)
    try:
        if row.kind == "hasm":
            source = project.src / f"{unit}.s"
            with scratch.temporary(host, project, "land", prefix="push-asm-") as temporary:
                data = original_asm.assemble(project, host, source.read_text(), row, Path(temporary))
            original_asm.prove(project, row, data)
            equal = data == split.words(project, row)
        elif unit in attempts.ledger(project).fuzzy_sources():
            source = project.src / f"{unit}.c"
            measured, _ = land._fuzzy_builds_row((project, project, host, unit, source, version))
            equal = measured["compiled"]
        else:
            equal, _ = land._builds_row((project, project, host, unit, project.src / f"{unit}.c", version))
        if not equal:
            raise Held(
                cause_named(
                    "publish.push_proof",
                    f"publish.push_proof: {unit} VERSION {version}: rebased native proof differs",
                    owner="project.publication_push",
                    stage="publish",
                )
            )
    except Held as error:
        return {
            "function": unit,
            "version": version,
            "key": error.key,
            "reason": error.reason,
            "fault": capture(
                error,
                cause=cause_named(
                    "project.publication_push.unexpected", str(error), owner="project.publication_push", stage="project"
                ),
            ).document(),
        }
    return None


def reconcile(
    project: Project, host: Host, before: dict[ProofKey, str]
) -> tuple[list[dict[str, str]], dict[ProofKey, str]]:
    from unbake.report import state

    state.inventory(project)
    after = snapshot(project, host)
    affected = [key for key, digest in after.items() if before.get(key) != digest]
    # Source admission remains a publication gate after the rebase. Scan each
    # affected C unit once, before starting native workers for its versions.
    sources = tuple(
        sorted({project.src / f"{unit}.c" for unit, _ in affected if (project.src / f"{unit}.c").is_file()})
    )
    if sources:
        from unbake.decomp import checks

        fuzzy = attempts.ledger(project).fuzzy_sources()
        findings = checks.findings(project, sources, Cache(project.cache))
        blocked = [row for row in findings.rows if Path(row.path).stem in fuzzy or row.finding.fakematch is None]
        if blocked:
            first = blocked[0]
            detail = checks.plain(first.finding)
            raise Held(
                cause_named(
                    "publish.push_rules",
                    f"publish.push_rules: {first.path}:{first.finding.line}: {detail}; nothing pushed",
                    owner="project.publication_push",
                    stage="publish",
                )
            )
    failures = (
        tuple(
            failure
            for failure in pool.run(host, _prove, [(project, host, unit, version) for unit, version in affected])
            if failure is not None
        )
        if affected
        else ()
    )
    if failures:
        raise Held(
            cause_named(
                "publish.push_proof",
                "publish.push_proof: affected rebased proofs failed; nothing pushed",
                owner="project.publication_push",
                stage="publish",
            ),
            failures=failures,
        )
    return [{"function": unit, "version": version} for unit, version in affected], after


def resolve_conflicts(project: Project, host: Host) -> bool:
    """Union immutable events and rerender owned reports during the existing rebase.

    Authored/source conflicts retain Git's refusal. README is eligible only if
    both branches agree outside the owning progress section.
    """
    from unbake import buildfiles
    from unbake.report import progress, readme_layout, verify

    conflicts = tuple(p for p in _git(project, "diff", "--name-only", "--diff-filter=U", "-z").split("\0") if p)
    generated = {verify.BUNDLE, ".github/workflows/progress.yml", ".gitlab-ci.yml"}
    allowed = {
        attempts.PATH,
        verify.MANIFEST,
        "README.md",
        *generated,
        *("versions/" + v + "/report.json" for v in project.versions),
    }
    if not conflicts or set(conflicts) - allowed:
        return False
    with journal.transaction(project):
        if attempts.PATH in conflicts:
            sides = [_git(project, "show", ":" + str(stage) + ":" + attempts.PATH).encode() for stage in (1, 2, 3)]
            merged = attempts.Ledger.merge(*sides)
            atomic.write(project.root / attempts.PATH, merged)
            # Strict readback owns both project identity and current-source projection.
            history = attempts.ledger(project)
            history._refresh()
            expected = {strict_json.loads(line, "merged ledger")["event_id"] for line in merged.splitlines()}
            if set(history.events) != expected:
                raise Held(
                    cause_named(
                        "ledger.merge_readback",
                        "concurrent event union differs on readback",
                        owner="work.attempts",
                        stage="publish",
                    )
                )
        if "README.md" in conflicts:
            ours, theirs = (_git(project, "show", ":" + str(stage) + ":README.md") for stage in (2, 3))
            left, _, right = readme_layout.section(ours)
            other_left, _, other_right = readme_layout.section(theirs)
            if (left, right) != (other_left, other_right):
                raise Held(
                    cause_named(
                        "publish.readme_conflict",
                        "authored README sections conflict; preserve both branches",
                        owner="project.publication_push",
                        stage="publish",
                    )
                )
            atomic.text(project.root / "README.md", ours)
        if verify.MANIFEST in conflicts:
            # The old report manifest is a projection, never history authority.
            atomic.text(project.root / verify.MANIFEST, _git(project, "show", ":2:" + verify.MANIFEST))
        for name in conflicts:
            if name.startswith("versions/") and name.endswith("/report.json"):
                atomic.text(project.root / name, _git(project, "show", ":2:" + name))
        current = config.load(project.root)
        written = buildfiles.write_progress(current, publish_branch=host.publish_branch)
        written.extend(progress.write(current, host, source_only=True))
        verify.validate(current)
        _git(project, "add", "--", *sorted({*conflicts, *(p.relative_to(project.root).as_posix() for p in written)}))
    return True


def rebase(project: Project, host: Host) -> None:
    try:
        _git(project, "rebase", "FETCH_HEAD")
        return
    except Held as error:
        original = error
    while resolve_conflicts(project, host):
        try:
            _git(project, "-c", "core.editor=true", "rebase", "--continue")
            return
        except Held:
            if not _git(project, "diff", "--name-only", "--diff-filter=U", "-z"):
                raise
    raise original


def push(project: Project, host: Host, remote: str, *, attempts_limit: int = 5) -> dict[str, Any]:
    """Fetch, rebase when needed, reprove affected scopes, and push without force.

    No persistent proof or resumable session is introduced. Each lost push race
    reconciles the next remote delta against the immediately prior proved view.
    An unfinished proof restores the prior local commit for a fresh retry.
    """
    if not remote or remote.startswith("-"):
        raise Held(
            cause_named(
                "publish.push_remote",
                "publish.push_remote: supply a remote name or URL",
                owner="project.publication_push",
                stage="publish",
            )
        )
    branch = host.publish_branch
    _git(project, "check-ref-format", "refs/heads/" + branch)
    from unbake.report import state

    attempts.ledger(project).assert_portable()
    state.inventory(project)
    before: dict[ProofKey, str] | None = None
    reconciled = []
    original = _git(project, "rev-parse", "HEAD")
    for _ in range(attempts_limit):
        _git(project, "fetch", "--", remote, branch)
        tip = _git(project, "rev-parse", "FETCH_HEAD")
        base = _git(project, "merge-base", "HEAD", "FETCH_HEAD")
        if base != tip:
            if _git(project, "status", "--porcelain", "--untracked-files=no"):
                raise Held(
                    cause_named(
                        "publish.push_dirty",
                        "publish.push_dirty: commit tracked edits before rebasing publication",
                        owner="project.publication_push",
                        stage="publish",
                    )
                )
            if before is None:
                before = snapshot(config.load(project.root), host)
            prior = _git(project, "rev-parse", "HEAD")
            try:
                rebase(project, host)
            except KeyboardInterrupt:
                try:
                    _git(project, "rebase", "--abort")
                except Held:
                    _git(project, "reset", "--keep", prior)
                raise
            except Held as error:
                _git(project, "rebase", "--abort")
                raise Held(
                    capture(
                        error,
                        cause=cause_named(
                            "publish.push_conflict",
                            "publish.push_conflict: concurrent changes conflict; local commits retained",
                            owner="project.publication_push",
                            stage="publish",
                        ),
                    )
                ) from error
            try:
                current = config.load(project.root)
                proved, before = reconcile(current, host, before)
            except BaseException:
                # Retain the pre-rebase local commit on any unfinished proof.
                # A fresh retry will see the same remote divergence and must
                # reconcile again, without a second proof/resume ledger.
                _git(project, "reset", "--keep", prior)
                raise
            reconciled.extend(proved)
        attempts.ledger(config.load(project.root)).assert_portable()
        try:
            _git(project, "push", "--", remote, "HEAD:refs/heads/" + branch)
        except Held as error:
            # A transport/authentication refusal is not a concurrent publication.
            # Only a newly fetched remote tip authorizes another reconciliation.
            _git(project, "fetch", "--", remote, branch)
            if _git(project, "rev-parse", "FETCH_HEAD") == tip:
                raise Held(
                    capture(
                        error,
                        cause=cause_named(
                            "publish.push_transport",
                            "publish.push_transport: remote refused push; local commits retained",
                            owner="project.publication_push",
                            stage="publish",
                        ),
                    )
                ) from error
            continue
        return {
            "remote": remote,
            "branch": branch,
            "before": original,
            "head": _git(project, "rev-parse", "HEAD"),
            "reconciled": reconciled,
        }
    raise Held(
        cause_named(
            "publish.push_race",
            "publish.push_race: remote moved repeatedly; local commits retained, retry publish --push",
            owner="project.publication_push",
            stage="publish",
        )
    )
