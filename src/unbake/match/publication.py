from __future__ import annotations

import fcntl
import json
import os
import shutil
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from unbake.decomp import checks
from unbake.match import staging
from unbake.match.common import (
    Attempt,
    Draft,
    atomic,
    held,
    queue,
    queue_lock,
    queue_path,
    read,
    sha,
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
    lock_path = project.root / "build" / ".lock"
    with lock_path.open("a+b") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        for version, generation in current.items():
            if build.current_generation(project, version).resolve() != generation:
                held(f"VERSION {version}: current generation changed during match build")
        latest = staging.fingerprint(project.root)
        if latest != fingerprint:
            changed = sorted(
                key for key in latest.keys() | fingerprint.keys() if latest.get(key) != fingerprint.get(key)
            )
            held(f"project build inputs changed during match build: {', '.join(changed)}")
        with queue_lock(project, policy):
            rows = queue(project)
            for draft in candidates:
                if draft.row not in rows:
                    held(f"{draft.function}: queue row changed or withdrawn during match build")
                if sha(read(Path(draft.row["source"]))) != draft.row["source_sha256"]:
                    held(f"{draft.function}: source_sha256 changed during match build")
            writes = {}
            for edit in attempt.edits:
                destination = project.root / Path(edit.path).relative_to(attempt.tree)
                writes[destination] = read(edit.path)
            ledger = Path(policy.state_root) / project.name / "receipts" / "match.jsonl"
            reports = {project.root / "versions" / version / "report.json" for version in project.versions}
            touched = set(writes) | {ledger, queue_path(project), project.root / "README.md"} | reports
            before = {path: read(path) if path.exists() else None for path in touched}
            swapped = []
            try:
                for path, content in writes.items():
                    atomic(path, content)
                for version, generation in attempt.generations.items():
                    swap(project.build_link(version), generation)
                    swapped.append(version)
                progress.write(project, policy)
                ledger.parent.mkdir(parents=True, exist_ok=True)
                with ledger.open("a", encoding="utf-8") as output:
                    for draft in candidates:
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
    parent = project.root / "build"
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
