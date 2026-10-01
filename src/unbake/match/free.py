"""Land matching published drafts for an explicitly selected VERSION.

Run with python -m unbake.match.free --project ROOT --version VERSION.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from unbake.cli.common import receipt
from unbake.match import queue
from unbake.project.config import Held, Policy, Project, load, load_policy


def land(project: Project, policy: Policy, version: str) -> list[str]:
    """Try each partial in this VERSION and publish all accepted drafts together."""
    project.version(version)
    messages = []
    text = project.version(version).split.read_text()
    assembly = {entry[1] for line in text.splitlines() if (entry := queue.row(line)) and entry[0] == "asm"}
    for source in sorted(project.src.glob("*.c")):
        try:
            if source.stem not in assembly:
                continue
            messages.extend(queue.submit(project, policy, source, versions=(version,)))
        except Held as error:
            if error.phase == "match" and "requires identical_everywhere=true" in error.reason:
                messages.append(f"OK(match): skipped {source.stem}: {error.reason}")
            else:
                messages.append(f"HELD(match): {source.stem}: {error.reason}")
    if queue.status(project=project, policy=policy):
        messages.extend(queue.run(project, policy))
    return messages


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--policy", type=Path)
    args = parser.parse_args()
    try:
        refused = receipt("match", land(load(args.project), load_policy(args.policy), args.version))
    except Held as error:
        print(f"HELD({error.phase}): {error.reason}")
        raise SystemExit(1) from error
    raise SystemExit(int(refused))


if __name__ == "__main__":
    main()
