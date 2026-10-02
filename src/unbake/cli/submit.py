"""Validate, prove and publish tried sources transactionally."""

import argparse
from pathlib import Path

from unbake.cli.common import Subparsers, receipt, suggest
from unbake.cli.guidance import command
from unbake.match import queue
from unbake.project.config import Held, Policy, Project, Unfinished, load_policy


def register(phases: Subparsers) -> None:
    parser = phases.add_parser("submit", phase="submit", help="Prove and publish tried sources and its headers.")
    parser.add_argument("source", type=Path, metavar="FILE", nargs="?")
    parser.add_argument("--batch", type=Path, nargs="+", metavar="FILE")


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
    lines = (
        queue.publish_sources(project, policy, sources)
        if batch is not None
        else queue.publish_source(project, policy, args.source)
    )
    suggest(command(project.root, "next"))
    return receipt("submit", lines)
