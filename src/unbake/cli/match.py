"""The match command arguments and execution."""

import argparse
from pathlib import Path

from unbake.cli.common import Subparsers, receipt
from unbake.project.config import Policy, Project


def register(phases: Subparsers) -> None:
    match = phases.add_parser("match", phase="match", help="Queue and prove C matches through make compare.")
    match_verbs = match.add_subparsers(dest="verb", required=True)
    submit = match_verbs.add_parser("submit", phase="match")
    submit.add_argument("source", type=Path, metavar="FILE")
    withdraw = match_verbs.add_parser("withdraw", phase="match")
    withdraw.add_argument("function")
    match_verbs.add_parser("run", phase="match")
    match_verbs.add_parser("status", phase="match")


def run(args: argparse.Namespace, project: Project, policy: Policy) -> bool:
    from unbake.match import queue as match

    if args.verb == "submit":
        match.submit(project, policy, args.source)
        return receipt("match", [f"submitted {args.source}"])
    if args.verb == "withdraw":
        match.withdraw(args.function, project=project)
        return receipt("match", [f"withdrew {args.function}"])
    if args.verb == "run":
        return receipt("match", match.run(project, policy))
    return receipt("match", match.status(project=project))
