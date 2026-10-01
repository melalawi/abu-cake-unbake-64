"""The report command arguments and execution."""

import argparse

from unbake.cli.common import Subparsers, receipt
from unbake.project import build
from unbake.project.config import Policy, Project


def register(phases: Subparsers) -> None:
    phases.add_parser("report", phase="report", help="Generate objdiff reports and project progress.")


def run(args: argparse.Namespace, project: Project, policy: Policy) -> bool:
    from unbake.report import progress as report

    with build.lock(project):
        return receipt("report", report.write(project, policy))
