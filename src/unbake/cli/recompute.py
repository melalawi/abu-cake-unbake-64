"""recompute: re-run an internal step and ignore its cached results (debugging only)."""

from __future__ import annotations

import argparse

from unbake.cli.args import Context
from unbake.cli.output import Result

NAME = "recompute"
HELP = "Re-run internal steps, ignoring their cached results (debugging)."
DESCRIPTION = """\
Re-run the named internal steps (or all) and ignore their cached results. For debugging only; ordinary
work never needs it because every step reruns by itself when its inputs change.

  unbake recompute types
  unbake recompute headers buildfiles
  unbake recompute --all
"""
PROJECT = "ready"


def READ_ONLY(args: argparse.Namespace) -> bool:
    return False


def register(parser: argparse.ArgumentParser) -> None:
    from unbake import steps

    parser.add_argument("steps", nargs="*", metavar="STEP", help="One or more of: " + ", ".join(steps.NAMES))
    parser.add_argument("--all", action="store_true")


def run(context: Context) -> Result:
    from unbake import steps
    from unbake.config import Held

    named = tuple(dict.fromkeys(context.args.steps))
    if context.args.all == bool(named):
        raise Held("recompute", "recompute.steps: name one or more steps, or --all")
    unknown = [name for name in named if name not in steps.NAMES]
    if unknown:
        raise Held("recompute", f"recompute.steps: unknown {', '.join(unknown)}; steps are {', '.join(steps.NAMES)}")
    names = steps.NAMES if context.args.all else named
    ran = steps.recompute(context.project(), context.require_host(), names)
    data = {"steps": [row.document() for row in ran]}
    findings = [f"OVER {finding}" for row in ran for finding in row.findings]
    return Result.ok(NAME, data, [*findings, *(note for row in ran for note in row.contended)], context.cmd("next"))
