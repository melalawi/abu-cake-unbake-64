"""The unbake command tree and phase dispatch."""

import argparse
import os
import sys
from pathlib import Path

from unbake.cli import check, clone, decomp, init, match, report, setup, split
from unbake.cli.common import Parser
from unbake.project import config
from unbake.project.config import Held, Policy, Project


def make_parser() -> argparse.ArgumentParser:
    parser = Parser(prog="unbake", description="Build and match N64 decompilation projects.")
    parser.add_argument("--project", type=Path, metavar="DIR")
    parser.add_argument("--policy", type=Path, metavar="FILE")
    phases = parser.add_subparsers(dest="phase", required=True)
    for command in (setup, init, split, decomp, match, report, check, clone):
        command.register(phases)
    return parser


def dispatch(args: argparse.Namespace, project: Project, policy: Policy) -> bool:
    if args.phase == "decomp":
        decomp.run(args, project, policy)
        return False
    commands = {
        "clone": clone.run,
        "setup": setup.run,
        "split": split.run,
        "match": match.run,
        "report": report.run,
        "check": check.run,
    }
    return commands[args.phase](args, project, policy)


def main(argv: list[str] | None = None) -> int:
    phase = "config"
    try:
        args = make_parser().parse_args(argv)
        phase = args.phase
        if args.policy is not None:
            os.environ["UNBAKE_POLICY"] = str(args.policy.expanduser().absolute())
        if args.phase == "init":
            return 1 if init.run(args) else 0
        if args.project is None:
            raise Held("config", "--project: missing value")
        project = config.load(args.project)
        policy = config.load_policy()
        return 1 if dispatch(args, project, policy) else 0
    except Held as error:
        print(f"HELD({error.phase}): {error.reason}", file=sys.stderr)
        return 1
    except (ImportError, OSError) as error:
        print(f"HELD({phase}): {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
