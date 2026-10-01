"""Compare an editable source using configured work and holding versions."""

import argparse
from pathlib import Path

from unbake.cli.common import Subparsers, receipt
from unbake.decomp import trial
from unbake.project.config import Policy, Project, Unfinished, load_policy


def register(phases: Subparsers) -> None:
    parser = phases.add_parser("try", phase="try", help="Compile and retain exact current-input trial evidence.")
    parser.add_argument("source", type=Path, metavar="FILE")
    parser.add_argument("--flags", action="store_true", help="Measure explicit compiler flag alternatives.")


def run(args: argparse.Namespace, project: Project, policy: Policy) -> bool:
    if args.source.suffix == ".h":
        raise Unfinished("try", "trial.struct")
    local = project.tools / "clone-policy.toml"
    if local.is_file():
        policy = load_policy(local)
    result = trial.retain_draft(project, policy, args.source, project.work, versions=None, flags=args.flags)
    return receipt("try", [f"retained {result.function} source_sha256 {result.source_sha256}"])
