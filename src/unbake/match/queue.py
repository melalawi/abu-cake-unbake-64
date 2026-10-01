from __future__ import annotations

import re
import shutil
import tempfile
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import replace
from pathlib import Path
from typing import Any

from unbake.decomp import checks, drafts, features, needs
from unbake.decomp.source_function import select
from unbake.layout import split
from unbake.match import common, declarations, proof, xver
from unbake.match import staging as stage
from unbake.match.common import (
    Draft,
    function,
    held,
    queue,
    queue_lock,
    queue_path,
    read,
    relative,
    sha,
    write_queue,
)
from unbake.match.publication import collect, publish
from unbake.match.staging import bisect, copy_tree
from unbake.project import build
from unbake.project.config import Held, Policy, Project

_ROW = re.compile(
    r"^(?P<head>\s*-\s*\[\s*(?:0[xX][0-9a-fA-F]+|[0-9]+)\s*,\s*)"
    r"(?P<kind>asm|c)(?P<gap>\s*,\s*)"
    r"(?P<name>[^,\]\r\n]+?)(?P<tail>\s*\]\s*)$"
)


def row(line: str) -> tuple[str, str] | None:
    """Read the kind and source name of a code split row."""
    match = _ROW.fullmatch(line)
    return None if match is None else (match["kind"], Path(match["name"].strip().strip("\"'")).name)


def holding_versions(project: Project, function: str) -> tuple[str, ...]:
    versions = []
    for version in project.versions:
        path = project.version(version).split
        for line in read(path).decode().splitlines():
            row = _ROW.fullmatch(line)
            if row and Path(row["name"].strip().strip("\"'")).name == function:
                versions.append(version)
                break
    if not versions:
        held(f"{function}: split row missing in every VERSION")
    return tuple(versions)


def validate(project: Project, policy: Policy, row: dict[str, Any]) -> Draft:
    function = common.function(row["function"])
    source = Path(row["source"])
    if source.suffix != ".c" or source.stem != function:
        held(f"{function}: source {source} must be named {function}.c")
    content = read(source)
    sha = drafts.source_identity(content)
    if sha != row["source_sha256"]:
        held(f"{function}: {source} source_sha256 changed since submit")
    records = drafts.Store(policy, project).rows(function)
    proof = [record for record in records if record.get("source_sha256") == sha]
    if not proof:
        held(f"{function}: drafts.Store trial row missing source_sha256 {sha}")
    latest = proof[-1]
    if latest.get("identical_everywhere") is not True:
        held(f"{function}: trial source_sha256 {sha} requires identical_everywhere=true")
    holding = holding_versions(project, function)
    versions = tuple(row.get("versions", holding))
    if not versions or any(v not in holding for v in versions):
        held(f"{function}: queue VERSIONs must select holding VERSIONs")
    if not isinstance(latest.get("compares"), dict) or not all(v in latest["compares"] for v in versions):
        held(f"{function}: trial compares missing VERSION proof for {', '.join(versions)}")
    try:
        text = content.decode("utf-8")
    except UnicodeError as error:
        held(f"{source}: {error}")
    findings = checks.run(text)
    blockers = [finding for finding in findings if finding.fakematch is None]
    if blockers:
        held(f"{function}: " + "; ".join(f"{finding.rule}:{finding.line}: {finding.text}" for finding in blockers))
    edits: list[split.Edit] = declarations.match_edits(project, function, text, versions)
    for edit in edits:
        relative(project, edit.path)
    pending: list[needs.Need] = list(findings)
    return Draft(row, content, versions, pending)


def submit(
    project: Project,
    policy: Policy,
    source: str | Path | None,
    *,
    versions: tuple[str, ...] | None = None,
    function_name: str | None = None,
) -> list[str]:
    """Enqueue canonical source bytes judged by their latest explicit trial."""
    if source is None:
        held("source: missing value")
    features.load()
    source = Path(source).resolve()
    name = select(source, read(source).decode("utf-8"), function_name, phase="match")
    source = proof.source(project, source, name)
    selected = holding_versions(project, function(source.stem)) if versions is None else versions
    for version in selected:
        project.version(version)
    proof.ensure(project, policy, source, selected)
    row: dict[str, Any] = {"function": function(source.stem), "source": str(source), "source_sha256": sha(read(source))}
    if versions is not None:
        row["versions"] = list(versions)
    stage.compile_fold(project, policy, validate(project, policy, row))
    with queue_lock(project):
        rows = [existing for existing in queue(project) if existing["function"] != row["function"]]
        rows.append(row)
        write_queue(project, rows)
    return [f"OK(match): {row['function']} queued {row['source_sha256']}"]


def withdraw(function: str, *, project: Project, policy: Policy | None = None) -> list[str]:
    """Remove one explicitly named function from this project's queue."""
    common.function(function)
    with queue_lock(project):
        rows = queue(project)
        remaining = [row for row in rows if row["function"] != function]
        if len(remaining) == len(rows):
            held(f"{function}: not in {queue_path(project)}")
        write_queue(project, remaining)
    return [f"OK(match): {function} withdrawn"]


def status(*, project: Project, policy: Policy | None = None) -> list[str]:
    """Return the current queue without deriving any project selection."""
    with queue_lock(project):
        rows = queue(project)
    return [f"OK(match): {row['function']} queued {row['source_sha256']} {row['source']}" for row in rows]


@contextmanager
def runner(project: Project) -> Iterator[Path]:
    staging = project.root / "build" / "match"
    staging.mkdir(parents=True, exist_ok=True)
    yield staging


class _Generations(dict[str, Path]):
    """Hold current generations as staging discovers VERSIONs from resolved edits."""

    def __init__(self, project: Project, holds: ExitStack) -> None:
        super().__init__()
        self.project = project
        self.holds = holds

    def __missing__(self, version: str) -> Path:
        try:
            with build.lock(self.project):
                generation = self.holds.enter_context(build.pin(build.current_generation(self.project, version)))
        except (Held, OSError) as error:
            reason = error.reason if isinstance(error, Held) else str(error)
            held(f"VERSION {version}: cannot acquire generation: {reason}")
        self[version] = generation
        return generation


def run(project: Project, policy: Policy) -> list[str]:
    """Build outside build/.lock, isolate failures, then publish verified files."""
    features.load()
    receipts: list[str] = []
    with queue_lock(project):
        rows = queue(project)
    candidates = []
    for row in rows:
        try:
            candidates.append(xver.expand(project, validate(project, policy, row), receipts))
        except Held as error:
            receipts.append(f"HELD(match): {row['function']}: {error.reason}")
    if not candidates:
        if not rows and (project.root / "build").is_dir():
            with runner(project):
                collect(project)
        return receipts
    workspace = None
    attempt = None
    published = False
    try:
        with runner(project) as staging:
            with ExitStack() as holds:
                current = _Generations(project, holds)
                workspace = Path(tempfile.mkdtemp(prefix="run-", dir=staging))
                base = workspace / "base"
                copy_tree(project.root, base)
                fingerprint = stage.fingerprint(base)
                attempt = stage.attempt(project, policy, base, workspace, current, candidates)
                if attempt.failures:
                    if len(candidates) == 1:
                        detail = "; ".join(attempt.diagnostics.values())
                        receipts.append(f"HELD(match): {candidates[0].function}: build compare failed on {detail}")
                        return receipts
                    attempt.discard()
                    attempt = None
                    middle = max(1, len(candidates) // 2)
                    accepted = bisect(project, policy, base, workspace, current, candidates[:middle], [], receipts)
                    if candidates[middle:]:
                        accepted = bisect(
                            project, policy, base, workspace, current, candidates[middle:], accepted, receipts
                        )
                    candidates = accepted
                    if not candidates:
                        return receipts
                    attempt = stage.attempt(project, policy, base, workspace, current, candidates)
                    if attempt.failures:
                        held("final build compare failed on " + "; ".join(attempt.diagnostics.values()))
                publish(project, policy, attempt, candidates, current, fingerprint)
                receipts.extend(f"OK(match): resolved need {name}" for name in attempt.resolved)
                published = True
            collect(replace(project, versions=tuple(current)))
        receipts.extend(
            f"OK(match): {draft.function} matched on VERSION {', '.join(draft.versions)}" for draft in candidates
        )
        return receipts
    except Held as error:
        receipts.extend(f"HELD(match): {draft.function}: {error.reason}" for draft in candidates)
        return receipts
    except OSError as error:
        held(str(error))
    finally:
        if attempt is not None:
            attempt.holds.close()
        if attempt is not None and (not published):
            attempt.discard()
        if workspace is not None:
            shutil.rmtree(workspace, ignore_errors=True)
