"""Apply the tracked declaration ownership map."""

import argparse

from unbake.cli.common import Subparsers, receipt
from unbake.layout import apply
from unbake.config import Host, Project


def register(phases: Subparsers) -> None:
    parser = phases.add_parser("layout", phase="layout", help="Render mapped declaration headers and source imports.")
    verbs = parser.add_subparsers(dest="verb", required=True)
    command = verbs.add_parser("apply")
    command.add_argument("--dry-run", action="store_true")


def run(args: argparse.Namespace, project: Project, policy: Host) -> bool:
    count = apply.run(project, policy, dry_run=args.dry_run)
    return receipt("layout", [f"{'would write' if args.dry_run else 'wrote'} {count} files"])
