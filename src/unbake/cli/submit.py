"""Validate, prove and publish one exact tried source transactionally."""

import argparse
from pathlib import Path

from unbake.cli.common import Subparsers, receipt, suggest
from unbake.cli.guidance import command
from unbake.match import queue
from unbake.project.config import Policy, Project, Unfinished


def register(phases: Subparsers) -> None:
    parser = phases.add_parser(
        "submit", phase="submit", help="Prove and publish an exact tried source and its headers."
    )
    parser.add_argument("source", type=Path, metavar="FILE")


def run(args: argparse.Namespace, project: Project, policy: Policy) -> bool:
    if args.source.suffix == ".h":
        raise Unfinished("submit", "submit.struct")
    lines = queue.publish_source(project, policy, args.source)
    suggest(command(project.root, "next"))
    return receipt("submit", lines)
