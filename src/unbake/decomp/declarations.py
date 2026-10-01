"""Promote reviewed local aggregate declarations into shared project headers."""

import argparse
from pathlib import Path

from unbake.layout import shared, split_apply
from unbake.layout.structs import layouts
from unbake.layout.structs_fold import fold
from unbake.project.config import Held, Policy, Project, load, load_policy


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


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True, type=Path)
    parser.add_argument("--policy", type=Path)
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--version", required=True)
    args = parser.parse_args()
    project, policy = load(args.project), load_policy(args.policy)
    try:
        print(promote(project, policy, args.source, args.version))
    except Held as error:
        parser.exit(1, f"HELD({error.phase}): {error.reason}\n")


if __name__ == "__main__":
    main()
