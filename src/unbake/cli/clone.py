"""Create an isolated workspace using the project's warm generations."""

import argparse
from pathlib import Path

from unbake.cli.common import Subparsers, receipt
from unbake.project.config import Held, Policy, Project


def register(phases: Subparsers) -> None:
    parser = phases.add_parser("clone", phase="clone", help="Copy a warm project into an isolated Git clone.")
    parser.add_argument("destination", type=Path, metavar="DIR")
    parser.add_argument("extra_destinations", nargs="*", help=argparse.SUPPRESS)
    parser.add_argument(
        "--version", action="append", metavar="VERSION", help="Repeat to select versions; omit for all."
    )


def validate(args: argparse.Namespace) -> None:
    if args.project is None or args.extra_destinations:
        raise Held("clone", "CLI form: unbake --project SRC clone DEST [--version VERSION]")


def run(args: argparse.Namespace, project: Project, policy: Policy) -> bool:
    from unbake.project import clone

    result = clone.create(project, policy, args.destination, args.version or project.versions)
    return receipt("clone", [f"{result.root}: warm VERSIONs {', '.join(args.version or project.versions)}"])
