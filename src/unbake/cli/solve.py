"""Solve project types, exposing every unknown and conflict."""

import argparse

from unbake.cli import common
from unbake.project.config import Policy, Project
from unbake.typemap import redrafts, solve


def register(phases: common.Subparsers) -> None:
    phases.add_parser("solve", phase="solve", help="Solve mapped signatures, globals and shared layouts.")


def run(args: argparse.Namespace, project: Project, policy: Policy) -> bool:
    value = solve(project, policy)
    lines = [
        f"revision={value['revision']} unknown={len(value['unknown'])} conflicts={len(value['conflicts'])}; "
        "build/types/database.json"
    ]
    for kind in ("functions", "globals", "structs", "arrays"):
        records = value[kind]
        lines.append(f"{kind}: known={sum(row['state'] == 'known' for row in records.values())} total={len(records)}")
    lines.extend(f"{row['key']}: {row.get('alternatives', [])}" for row in value["conflicts"])
    lines.append(f"redraft={len(redrafts(project))}; unknown details are retained in the database")
    common.suggest("unbake next")
    return common.receipt("solve", lines)
