"""Retain missing trial evidence using the ordinary decomp trial path."""

from __future__ import annotations

import argparse
from pathlib import Path

from unbake.cli.decomp import trial
from unbake.decomp import drafts
from unbake.match.common import atomic, read, sha
from unbake.project.config import Policy, Project


def source(project: Project, policy: Policy, path: Path) -> Path:
    """Preserve published draft bytes outside their NON_MATCHING wrapper."""
    content = read(path)
    text = content.decode("utf-8")
    if not drafts.is_partial(text):
        return path
    content = drafts.unguard(text).encode("utf-8")
    retained = policy.state_root / project.name / "match-sources" / sha(content) / path.name
    atomic(retained, content)
    return retained


def ensure(project: Project, policy: Policy, source: Path, versions: tuple[str, ...]) -> None:
    """Use existing evidence for these bytes, otherwise run and retain a trial."""
    digest = sha(read(source))
    if any(row["source_sha256"] == digest for row in drafts.Store(policy, project).rows(source.stem)):
        return
    scratch = policy.state_root / project.name / "match-trials"
    trial(argparse.Namespace(scratch=scratch), project, policy, source, list(versions))
