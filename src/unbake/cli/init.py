"""Create the named repository shell."""

import argparse
from pathlib import Path

from unbake.cli.common import Subparsers, receipt


def register(phases: Subparsers) -> None:
    parser = phases.add_parser("init", phase="init", help="Create a named repository and ROM folder.")
    parser.add_argument("target", type=Path, metavar="NAME")
    parser.add_argument("--layout-cap", type=int, required=True, metavar="MEMBERS")


def run(args: argparse.Namespace) -> bool:
    from unbake.project import init

    return receipt("init", init.run(args.target, layout_cap=args.layout_cap))
