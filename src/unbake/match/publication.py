from __future__ import annotations

import fcntl
import json
import os
import shutil
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from unbake.decomp import checks, drafts
from unbake.match import reporting, staging
from unbake.match.common import (
    Attempt,
    Draft,
    atomic,
    held,
    queue,
    queue_lock,
    queue_path,
    read,
    write_queue,
)
from unbake.project import build
from unbake.project.config import Policy, Project
from unbake.report import progress


def swap(link: Path, target: Path) -> None:
    temporary = link.with_name(f".{link.name}.{uuid4().hex}")
    try:
        temporary.symlink_to(target.name)
        os.replace(temporary, link)
    finally:
        temporary.unlink(missing_ok=True)


def publish(
    project: Project,
    policy: Policy,
    attempt: Attempt,
    candidates: list[Draft],
    current: dict[str, Path],
    fingerprint: dict[str, str],
) -> None:
    staged = staging.project_at(project, attempt.tree)
    reports = {
        v: progress.measure(
            staged, policy, v, generation=attempt.generations[v] if v in attempt.generations else current[v]
        )
        for v in project.versions
    }
    with build.lock(project):
        for version, generation in current.items():
            if build.current_generation(project, version).resolve() != generation:
                held(f"VERSION {version}: current generation changed during match build")
        latest = staging.fingerprint(project, project.root)
        if latest != fingerprint:
            changed = sorted(
                key for key in latest.keys() | fingerprint.keys() if latest.get(key) != fingerprint.get(key)
            )
            held(f"project build inputs changed during match build: {', '.join(changed)}")
        from unbake.match import proof

        for draft in candidates:
            if draft.matched:
                proof.ensure(project, policy, Path(draft.row["source"]), draft.versions)
            else:
                from unbake.match.nonmatching import admit

                admit(project, policy, Path(draft.row["source"]))
        with queue_lock(project):
            rows = queue(project)
            for draft in candidates:
                if draft.row not in rows:
                    held(f"{draft.function}: queue row changed or withdrawn during match build")
                if drafts.source_identity(read(Path(draft.row["source"]))) != draft.row["source_sha256"]:
                    held(f"{draft.function}: source_sha256 changed during match build")
            writes = {}
            for edit in attempt.edits:
                destination = project.root / Path(edit.path).relative_to(attempt.tree)
                writes[destination] = read(edit.path)
            ledger = Path(policy.state_root) / project.id / project.workspace_id / "receipts" / "match.jsonl"
            report_paths = {project.root / "versions" / version / "report.json" for version in project.versions}
            touched = set(writes) | {ledger, queue_path(project), project.root / "README.md"} | report_paths
            before = {path: read(path) if path.exists() else None for path in touched}
            swapped = []
            try:
                for path, content in writes.items():
                    atomic(path, content)
                for version, generation in attempt.generations.items():
                    swap(project.build_link(version), generation)
                    swapped.append(version)
                progress.write(project, policy, reports=reports)
                ledger.parent.mkdir(parents=True, exist_ok=True)
                with ledger.open("a", encoding="utf-8") as output:
                    for draft in candidates:
                        if not draft.matched:
                            continue
                        row = {
                            "function": draft.function,
                            "versions": list(draft.versions),
                            "sha256": draft.row["source_sha256"],
                            "fakematch": list(checks.fakematches(draft.content.decode("utf-8"))),
                            "at": datetime.now(UTC).isoformat(),
                        }
                        output.write(json.dumps(row, sort_keys=True) + "\n")
                functions = {draft.function for draft in candidates}
                write_queue(project, [row for row in rows if row["function"] not in functions])
                reporting.record(
                    "published",
                    sources=[draft.function for draft in candidates],
                    generations={version: str(generation) for version, generation in attempt.generations.items()},
                )
            except BaseException:
                for version in swapped:
                    swap(project.build_link(version), current[version])
                for path, previous_content in before.items():
                    if previous_content is None:
                        path.unlink(missing_ok=True)
                    else:
                        atomic(path, previous_content)
                raise


def collect(project: Project) -> None:
    """Serialize discovery with reader pinning; never wait for active generations."""
    with build.lock(project):
        _collect(project)


def _collect(project: Project) -> None:
    parent = project.build
    for version in project.versions:
        live = build.current_generation(project, version).resolve()
        for generation in parent.glob(f"{version}.*"):
            suffix = generation.name.removeprefix(version + ".")
            if (
                not suffix.isdigit()
                or not generation.is_dir()
                or generation.is_symlink()
                or (generation.resolve() == live)
            ):
                continue
            with (generation / ".inuse").open("a+b") as lock:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    continue
                shutil.rmtree(generation)
