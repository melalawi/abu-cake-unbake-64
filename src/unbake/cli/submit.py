"""Validate, prove and publish tried sources transactionally."""

import argparse
from dataclasses import replace
from pathlib import Path

from unbake.cli.common import Subparsers, receipt, suggest
from unbake.cli.guidance import command
from unbake.match import reporting
from unbake.match.batch import publish
from unbake.project.config import Held, Policy, Project, Unfinished, load_policy


def register(phases: Subparsers) -> None:
    parser = phases.add_parser(
        "submit", phase="submit", help="Prove and publish sources, including edits to published C."
    )
    parser.add_argument("source", type=Path, metavar="FILE", nargs="?")
    parser.add_argument("--scratch", type=Path, help="Read receipts retained by try in this private directory.")
    parser.add_argument("--batch", type=Path, nargs="*", metavar="FILE")


def run(args: argparse.Namespace, project: Project, policy: Policy) -> bool:
    batch = getattr(args, "batch", None)
    if (args.source is None) == (batch is None):
        raise Held("submit", "submit.source: supply FILE or --batch FILE..., exclusively")
    sources = batch if batch is not None else [args.source]
    if any(source.suffix == ".h" for source in sources):
        raise Unfinished("submit", "submit.struct")
    local = project.tools / "clone-policy.toml"
    if local.is_file():
        policy = load_policy(local)
    if args.scratch is not None:
        policy = replace(policy, state_root=args.scratch.resolve() / "state")
    emitted: set[str] = set()

    def emit(line: str) -> None:
        emitted.add(line)
        receipt("submit", [line])

    with reporting.stream(emit):
        lines = publish(project, policy, sources)
    followups = [line.split("; follow-up: ", 1)[1] for line in lines if line.startswith("OK(types):")]
    suggest(followups[0] if followups else command(project.root, "next"))
    remaining = [line for line in lines if line not in emitted]
    if remaining or not lines:
        receipt("submit", remaining)
    return any(line.startswith("HELD(") for line in lines)
