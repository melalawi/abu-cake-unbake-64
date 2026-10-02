"""The bulk-name branch shared with the public split parser."""

from __future__ import annotations

import argparse

from unbake.cli.common import receipt
from unbake.layout import rename_map
from unbake.project.config import Held, Policy, Project


def run(args: argparse.Namespace, project: Project, policy: Policy) -> bool:
    if args.map is not None and (args.function is not None or args.new_name is not None):
        raise Held("split", "split.rename.map: --map cannot be combined with positional names")
    mapping = rename_map.read(args.map) if args.map is not None else {args.function: args.new_name}
    changes = rename_map.plan(project, mapping)
    if not args.apply:
        print(rename_map.diff(changes), end="")
        return receipt("split", [f"preview {len(mapping)} simultaneous renames in {len(changes)} files"])
    results = rename_map.apply(project, policy, mapping)
    return receipt("split", [f"{result.version}: {result.sha1_line}" for result in results])
