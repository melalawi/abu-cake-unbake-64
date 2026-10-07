"""Push a publication after reconciling the proofs reached by a concurrent update."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict
from pathlib import Path
from typing import Any

from unbake import config, pool, process, runner, scratch
from unbake.cache import Cache
from unbake.compilers import drivers
from unbake.config import Held, Host, Project
from unbake.layout import split
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
    fuzzy = attempts.fuzzy_sources(project)
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
        elif unit in attempts.fuzzy_sources(project):
            source = project.src / f"{unit}.c"
            measured, _ = land._fuzzy_builds_row((project, project, host, unit, source, version))
            equal = measured["compiled"]
        else:
            equal, _ = land._builds_row((project, project, host, unit, project.src / f"{unit}.c", version))
        if not equal:
            raise Held("publish", f"publish.push_proof: {unit} VERSION {version}: rebased native proof differs")
    except Held as error:
        return {
            "function": unit,
            "version": version,
            "key": error.key,
            "reason": error.reason,
            "fault": process.fault(error),
        }
    return None


def reconcile(
    project: Project, host: Host, before: dict[ProofKey, str]
) -> tuple[list[dict[str, str]], dict[ProofKey, str]]:
    after = snapshot(project, host)
    affected = [key for key, digest in after.items() if before.get(key) != digest]
    # Source admission remains a publication gate after the rebase. Scan each
    # affected C unit once, before starting native workers for its versions.
    sources = tuple(
        sorted({project.src / f"{unit}.c" for unit, _ in affected if (project.src / f"{unit}.c").is_file()})
    )
    if sources:
        from unbake.decomp import checks

        fuzzy = attempts.fuzzy_sources(project)
        findings = checks.findings(project, sources, Cache(project.cache))
        blocked = [row for row in findings.rows if Path(row.path).stem in fuzzy or row.finding.fakematch is None]
        if blocked:
            first = blocked[0]
            detail = checks.plain(first.finding)
            raise Held(
                "publish",
                f"publish.push_rules: {first.path}:{first.finding.line}: {detail}; nothing pushed",
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
        raise Held("publish", "publish.push_proof: affected rebased proofs failed; nothing pushed", failures=failures)
    return [{"function": unit, "version": version} for unit, version in affected], after


def push(project: Project, host: Host, remote: str, *, attempts_limit: int = 5) -> dict[str, Any]:
    """Fetch, rebase when needed, reprove affected scopes, and push without force.

    No persistent proof or resumable session is introduced. Each lost push race
    reconciles the next remote delta against the immediately prior proved view.
    An unfinished proof restores the prior local commit for a fresh retry.
    """
    if not remote or remote.startswith("-"):
        raise Held("publish", "publish.push_remote: supply a remote name or URL")
    branch = host.publish_branch
    _git(project, "check-ref-format", "refs/heads/" + branch)
    before: dict[ProofKey, str] | None = None
    reconciled = []
    original = _git(project, "rev-parse", "HEAD")
    for _ in range(attempts_limit):
        _git(project, "fetch", "--", remote, branch)
        tip = _git(project, "rev-parse", "FETCH_HEAD")
        base = _git(project, "merge-base", "HEAD", "FETCH_HEAD")
        if base != tip:
            if _git(project, "status", "--porcelain", "--untracked-files=no"):
                raise Held("publish", "publish.push_dirty: commit tracked edits before rebasing publication")
            if before is None:
                before = snapshot(config.load(project.root), host)
            prior = _git(project, "rev-parse", "HEAD")
            try:
                _git(project, "rebase", "FETCH_HEAD")
            except KeyboardInterrupt:
                try:
                    _git(project, "rebase", "--abort")
                except Held:
                    _git(project, "reset", "--keep", prior)
                raise
            except Held as error:
                _git(project, "rebase", "--abort")
                raise Held(
                    "publish", "publish.push_conflict: concurrent changes conflict; local commits retained"
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
        try:
            _git(project, "push", "--", remote, "HEAD:refs/heads/" + branch)
        except Held as error:
            # A transport/authentication refusal is not a concurrent publication.
            # Only a newly fetched remote tip authorizes another reconciliation.
            _git(project, "fetch", "--", remote, branch)
            if _git(project, "rev-parse", "FETCH_HEAD") == tip:
                raise Held("publish", "publish.push_transport: remote refused push; local commits retained") from error
            continue
        return {
            "remote": remote,
            "branch": branch,
            "before": original,
            "head": _git(project, "rev-parse", "HEAD"),
            "reconciled": reconciled,
        }
    raise Held("publish", "publish.push_race: remote moved repeatedly; local commits retained, retry publish --push")
