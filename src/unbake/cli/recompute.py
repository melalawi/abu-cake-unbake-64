"""recompute: re-run an internal step and ignore its cached results (debugging only)."""

from __future__ import annotations

import argparse

from unbake.cli.args import Context
from unbake.cli.output import Result

NAME = "recompute"
HELP = "Re-run an internal step, ignoring its cached results (debugging)."
HIDDEN = True
DESCRIPTION = """\
Re-run one internal step (or all) and ignore its cached results. For debugging only; ordinary work
never needs it because every step reruns by itself when its inputs change.

  unbake recompute types
  unbake recompute --all
"""
PROJECT = "ready"


def READ_ONLY(args: argparse.Namespace) -> bool:  # noqa: N802 - verb protocol
    return False


def register(parser: argparse.ArgumentParser) -> None:
    from unbake import steps

    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("step", nargs="?", choices=steps.NAMES)
    group.add_argument("--all", action="store_true")


def run(context: Context) -> Result:
    from unbake import steps

    names = steps.NAMES if context.args.all else (context.args.step,)
    ran = steps.recompute(context.project(), context.require_host(), names)
    data = {"steps": [row.document() for row in ran]}
    return Result.ok(NAME, data, [row.line() for row in ran], context.cmd("next"))
