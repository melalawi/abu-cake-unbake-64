"""Promote reviewed local aggregate declarations into shared project headers."""

from pathlib import Path

from unbake.layout import shared, split_apply
from unbake.layout.structs import layouts
from unbake.layout.structs_fold import fold
from unbake.project.config import Held, Policy, Project


def promote(project: Project, policy: Policy, source: Path, version: str) -> Path:
    records = layouts(source, project=project, policy=policy, version=version)
    headers = sorted({path for root in project.include for path in root.rglob("*.h")})
    known = {
        record.name for header in headers for record in layouts(header, project=project, policy=policy, version=version)
    }
    missing = [record for record in records if record.name not in known]
    if not missing:
        raise Held("declarations", f"{source}: no missing shared aggregate declarations")
    destination = shared.home(project)
    for edit in fold(missing, project):
        if edit.path.is_symlink():
            raise Held("declarations", f"{edit.path}: shared header must not be a symlink")
        split_apply.write(edit.path, edit.after)

    return destination
