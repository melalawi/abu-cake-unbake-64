"""Explicitly collect abandoned build workspaces and unpinned generations."""

import argparse

from unbake.cli.common import Subparsers, receipt
from unbake.project.config import Policy, Project


def register(phases: Subparsers) -> None:
    phases.add_parser("collect", phase="collect", help="Remove abandoned scratch and unpinned old generations.")


def run(args: argparse.Namespace, project: Project, policy: Policy) -> bool:
    from unbake.match.publication import collect

    collect(project)
    return receipt("collect", ["abandoned workspaces and unpinned old generations removed"])
