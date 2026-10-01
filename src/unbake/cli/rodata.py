"""Inspect constant ownership without changing project inputs."""

import argparse
import json

from unbake.cli.common import Subparsers
from unbake.project.config import Policy, Project


def register(phases: Subparsers) -> None:
    parser = phases.add_parser("rodata", phase="rodata", help="Inspect compiler constant ownership.")
    verbs = parser.add_subparsers(dest="verb", required=True)
    owners = verbs.add_parser("owners", phase="rodata", help="Read existing ROM, split, and ELF ownership evidence.")
    owners.add_argument("--version", metavar="V", help="Use the configured naming version when omitted.")


def run(args: argparse.Namespace, project: Project, policy: Policy) -> bool:
    from unbake.layout import rodata_owners

    version = project.names_from if args.version is None else args.version
    print(json.dumps(rodata_owners.scan(project, version).document(), indent=2))
    return False
