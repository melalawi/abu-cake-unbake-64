"""Serve one explicitly provisioned machine resource domain."""

from __future__ import annotations

import argparse
from pathlib import Path

from unbake import admission
from unbake.cli.args import Context
from unbake.cli.output import Result, line

NAME = "resources"
HELP = "Serve the shared CPU/memory admission domain."
DESCRIPTION = """\
Serve one parent-provisioned cgroup v2 domain, using an explicit manifest.
This only grants resources; existing Pool and native owners execute the work.
Every participating command must start in the domain's bounded control subtree.
Resource grants/releases stream as JSONL. A second broker for the same physical
cgroup refuses. Stopping the service terminates its granted command subtrees.
The service deployment must also kill its jobs on an abrupt broker exit.
"""
PROJECT = "none"


def READ_ONLY(args: argparse.Namespace) -> bool:
    return True


def register(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("domain", type=Path, metavar="MANIFEST")


def run(context: Context) -> Result:
    broker = admission.Broker(admission.Domain.read(context.args.domain), lambda event: line(context.stdout, event))
    broker.serve()
    return Result.ok(NAME, {}, [], None)
