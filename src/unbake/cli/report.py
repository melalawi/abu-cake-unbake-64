"""The report command arguments and execution."""

import argparse

from unbake.cli.common import Subparsers, receipt
from unbake.config import Host, Project


def register(phases: Subparsers) -> None:
    phases.add_parser("report", phase="report", help="Generate objdiff reports and project progress.")


def run(args: argparse.Namespace, project: Project, policy: Host) -> bool:
    from unbake.report import progress as report

    return receipt("report", report.write(project, policy))
