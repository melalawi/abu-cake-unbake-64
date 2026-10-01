"""The check command arguments and execution."""

import argparse

from unbake.cli.common import Subparsers, receipt
from unbake.project import hygiene
from unbake.project.config import Policy, Project
from unbake.report import progress


def register(phases: Subparsers) -> None:
    parser = phases.add_parser("check", phase="check", help="Check repository hygiene and C source evidence.")
    parser.add_argument("--hygiene", action="store_true", help="Check only tracked repository hygiene.")


def run(args: argparse.Namespace, project: Project, policy: Policy) -> bool:
    from unbake.decomp import checks

    lines = hygiene.tracked_findings(project, policy)
    if lines or getattr(args, "hygiene", False):
        return receipt("check", lines)
    lines.extend(progress.findings(project, policy))
    for source in sorted(project.src.rglob("*.c")):
        for finding in checks.run(source):
            location = f"{source.relative_to(project.root)}:{finding.line}: {finding.rule}: {finding.text}"
            lines.append(
                f"HELD(check): {location}"
                if finding.fakematch is None
                else f"OK(check): {location}; FAKEMATCH: {finding.fakematch}"
            )
    return receipt("check", lines)
