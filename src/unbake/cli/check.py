"""The check command arguments and execution."""

import argparse

from unbake.cli.common import Subparsers, receipt
from unbake.project.config import Policy, Project


def register(phases: Subparsers) -> None:
    phases.add_parser("check", phase="check", help="Check C sources and report FAKEMATCH evidence.")


def run(args: argparse.Namespace, project: Project, policy: Policy) -> bool:
    from unbake.decomp import checks

    lines = []
    for source in sorted(project.src.rglob("*.c")):
        for finding in checks.run(source):
            location = f"{source.relative_to(project.root)}:{finding.line}: {finding.rule}: {finding.text}"
            lines.append(
                f"HELD(check): {location}"
                if finding.fakematch is None
                else f"OK(check): {location}; FAKEMATCH: {finding.fakematch}"
            )
    return receipt("check", lines)
