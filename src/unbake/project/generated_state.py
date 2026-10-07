"""Reconcile disposable output receipts with committed project state."""

from __future__ import annotations

import hashlib
from pathlib import Path

from unbake import cache as retention
from unbake import inputs, process, steps
from unbake.config import Held, Project


def _git(project: Project, *args: str) -> str:
    return process.run_native(["git", *args], project.root, "steps", temporary_root=project.build).stdout


def reconcile(project: Project, step: str) -> list[str]:
    """Accept changed outputs only when HEAD proves their current bytes or deletion.

    Keep the generation input key; this does not claim a solve ran against the
    rebased inputs. Index-only edits, untracked files and local deletions remain
    altered. A project without a readable HEAD retains the original refusal.
    """
    changed = steps.altered(project, step)
    if not changed:
        return []
    try:
        tree = _git(project, "ls-tree", "-r", "-z", "HEAD", "--", *changed)
    except Held:
        return changed
    committed = {}
    tracked = set()
    for row in tree.split("\0"):
        if row:
            metadata, name = row.split("\t", 1)
            mode, kind, blob_id = metadata.split()
            tracked.add(name)
            if kind == "blob" and mode in ("100644", "100755"):
                committed[name] = blob_id
    entry = steps._read(project).get(step)
    if entry is None:
        return changed
    outputs = dict(entry.get("outputs", {}))
    refused = []
    for name in changed:
        path = project.root / name
        if Path(name).is_absolute() or ".." in Path(name).parts or path.resolve() != path:
            refused.append(name)
            continue
        ident = committed.get(name)
        if ident is not None and path.is_file():
            data = path.read_bytes()
            blob = b"blob " + str(len(data)).encode() + b"\0" + data
            digest = hashlib.sha256(blob) if len(ident) == 64 else hashlib.sha1(blob)
            if digest.hexdigest() == ident:
                outputs[name] = inputs.digest(path, algorithm="sha256", reuse=retention.configured())
                continue
        elif not path.exists():
            # Legacy receipts have no HEAD anchor. A committed deletion proves
            # the output used to be tracked; an untracked missing output cannot.
            try:
                deleted = _git(project, "log", "-1", "--diff-filter=D", "--format=%H", "HEAD", "--", name)
            except Held:
                deleted = ""
            if name not in tracked and deleted.strip():
                outputs.pop(name, None)
                continue
        refused.append(name)
    if outputs != entry.get("outputs", {}):
        steps.record(project, step, entry["key"], outputs)
    return refused
