"""Publish changed functions after comparing existing native linked bytes with ROM."""

from __future__ import annotations

import time
from contextlib import suppress
from pathlib import Path
from typing import Any

from unbake import atomic, config, journal, process, strict_json
from unbake.cache import Cache
from unbake.config import Held, Host, Project
from unbake.layout import split
from unbake.process import capture
from unbake.process import named as cause_named
from unbake.work import attempts


def _git(project: Project, *args: str) -> str:
    return process.run_tool(["git", *args], project.root, "publish").strip()


def admission(project: Project, host: Host, head: str, base: str) -> dict[str, Any]:
    """Only changed-function native linked bytes and relevant source hygiene."""
    started = time.perf_counter_ns()
    if _git(project, "status", "--porcelain"):
        raise Held(
            cause_named(
                "publish.push_dirty",
                "Commit owned edits before admission",
                owner="project.publication_push",
                stage="publish",
            )
        )
    changed = set(_git(project, "diff", "--name-only", "-z", base, head).split("\0")) - {""}
    sources = tuple(
        sorted(
            project.root / name
            for name in changed
            if (project.root / name).parent == project.src and Path(name).suffix in (".c", ".s")
        )
    )
    from unbake.decomp import checks

    findings = checks.findings(project, sources, Cache(project.cache))
    if findings.rows:
        raise Held(
            cause_named(
                "publish.push_rules",
                checks.plain(findings.rows[0].finding),
                owner="project.publication_push",
                stage="publish",
            )
        )
    scopes = []
    work = {"native_bytes_read": 0, "rom_bytes_read": 0, "functions_compared": 0}
    for version in project.versions:
        for row in split.functions(project, version):
            if row.kind not in ("c", "hasm"):
                continue
            unit = Path(row.path).name
            source = project.src / (unit + (".s" if row.kind == "hasm" else ".c"))
            if source not in sources:
                continue
            if not source.is_file():
                raise Held(
                    cause_named(
                        "publish.source_missing",
                        f"Changed source missing: {source.name}",
                        owner="project.publication_push",
                        stage="publish",
                    )
                )
            native = project.build_link(version) / ("hasm" if row.kind == "hasm" else "units") / (unit + ".bin")
            if not native.is_file():
                target = native.relative_to(project.root).as_posix()
                raise Held(
                    cause_named(
                        "publish.native_missing",
                        f"Native linked output missing; prepare: make -j12 {target}",
                        owner="project.publication_push",
                        stage="publish",
                    )
                )
            linked = native.read_bytes()
            original = split.words(project, row)
            if linked != original:
                raise Held(
                    cause_named(
                        "publish.native_mismatch",
                        f"{unit} {version}: native linked bytes differ from ROM slice",
                        owner="project.publication_push",
                        stage="publish",
                    )
                )
            scopes.append({"function": unit, "version": version, "bytes": len(linked)})
            work["native_bytes_read"] += len(linked)
            work["rom_bytes_read"] += len(original)
            work["functions_compared"] += 1
    return {
        "ok": True,
        "head": head,
        "base": base,
        "scopes": scopes,
        "source_findings": [],
        "work": work,
        "elapsed_ns": time.perf_counter_ns() - started,
    }


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

    The existing native CAS supplies current qualified linked extents. Every
    changed commit repeats admission; missing proof refuses without native work.
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
    started = time.perf_counter_ns()
    branch = host.publish_branch
    _git(project, "check-ref-format", "refs/heads/" + branch)
    original = _git(project, "rev-parse", "HEAD")
    validated = None
    checked: dict[str, Any] = {}
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
            try:
                rebase(project, host)
            except KeyboardInterrupt:
                with suppress(Held):
                    _git(project, "rebase", "--abort")
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
        current = config.load(project.root)
        head = _git(project, "rev-parse", "HEAD")
        if _git(project, "status", "--porcelain", "--untracked-files=no"):
            raise Held(
                cause_named(
                    "publish.push_dirty",
                    "commit tracked edits before the final check",
                    owner="project.publication_push",
                    stage="publish",
                )
            )
        if validated != (head, tip):
            untracked = _git(project, "ls-files", "--others", "--exclude-standard", "-z")
            checked = admission(current, host, head, tip)
            if (
                not isinstance(checked, dict)
                or checked.get("ok") is not True
                or checked.get("head") != head
                or checked.get("base") != tip
                or not isinstance(checked.get("scopes"), list)
            ):
                raise Held(
                    cause_named(
                        "publish.check_failed",
                        "Canonical admission did not pass for the final SHA",
                        owner="project.publication_push",
                        stage="publish",
                    ),
                    data={"check": checked, "head": head},
                )
            if (
                _git(project, "rev-parse", "HEAD") != head
                or _git(project, "status", "--porcelain", "--untracked-files=no")
                or _git(project, "ls-files", "--others", "--exclude-standard", "-z") != untracked
            ):
                raise Held(
                    cause_named(
                        "publish.check_changed",
                        "HEAD or owned files changed during the final check; nothing pushed",
                        owner="project.publication_push",
                        stage="publish",
                    ),
                    data={"check": checked, "head": head},
                )
            validated = (head, tip)
        try:
            _git(project, "push", "--", remote, head + ":refs/heads/" + branch)
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
            "head": head,
            "check": checked,
            "elapsed_ns": time.perf_counter_ns() - started,
            "reconciled": checked.get("affected_consumers", []),
        }
    raise Held(
        cause_named(
            "publish.push_race",
            "publish.push_race: remote moved repeatedly; local commits retained, retry publish --push",
            owner="project.publication_push",
            stage="publish",
        )
    )
