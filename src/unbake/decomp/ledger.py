"""Explicitly archive incompatible trial evidence before collecting new trials."""

import argparse
import fcntl
import json
from pathlib import Path

from unbake.project.config import Held


def archive(path: Path) -> Path:
    destination = path.with_name("trials.pre-needs.jsonl")
    with path.open("r+b") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        content = stream.read()
        rows = [json.loads(line) for line in content.splitlines()]
        if not rows or not all(isinstance(row, dict) for row in rows):
            raise Held("drafts", f"{path}: expected trial objects; archive refused")
        if not any("needs" not in row for row in rows):
            raise Held("drafts", f"{path}: no incompatible trials; archive refused")
        with destination.open("xb") as backup:
            backup.write(content)
            backup.flush()
        if destination.read_bytes() != content:
            raise Held("drafts", f"{destination}: archive readback differs; ledger preserved")
        stream.seek(0)
        stream.truncate()
        stream.flush()
        if path.read_bytes():
            raise Held("drafts", f"{path}: ledger readback is not empty")
    return destination


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ledger", type=Path, required=True)
    args = parser.parse_args()
    try:
        print(f"archived: {archive(args.ledger)}; rerun decomp try for each source before match submit")
    except (Held, OSError, ValueError) as error:
        parser.exit(1, f"HELD(drafts): {error}\n")


if __name__ == "__main__":
    main()
