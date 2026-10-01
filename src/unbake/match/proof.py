"""Select canonical source bytes and require retained trial evidence."""

from __future__ import annotations

from pathlib import Path

from unbake.decomp import drafts
from unbake.match.common import atomic, held, queue_path, read, sha
from unbake.project.config import Policy, Project


def source(project: Project, path: Path) -> Path:
    """Preserve published draft bytes outside their NON_MATCHING wrapper."""
    content = read(path)
    text = content.decode("utf-8")
    if not drafts.is_partial(text):
        return path
    content = drafts.canonical_source(content)
    retained = queue_path(project).parent / "match-sources" / sha(content) / path.name
    atomic(retained, content)
    return retained


def ensure(project: Project, policy: Policy, source: Path, versions: tuple[str, ...]) -> None:
    """Require an explicit trial for these exact canonical bytes."""
    digest = drafts.source_identity(read(source))
    if any(row["source_sha256"] == digest for row in drafts.Store(policy, project).rows(source.stem)):
        return
    held(f"{source.stem}: drafts.Store trial row missing source_sha256 {digest}; run decomp try {source}")
