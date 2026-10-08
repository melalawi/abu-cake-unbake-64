"""Explicit offline state migration, never entered by ordinary work."""

from __future__ import annotations

import argparse

from unbake.cli.args import Context
from unbake.cli.output import Result

NAME = "migrate-state"
HELP = "Plan or apply the one-time preserved state migration."
DESCRIPTION = HELP
PROJECT = "ready"


def READ_ONLY(args: argparse.Namespace) -> bool:
    return bool(args.plan)


def register(parser: argparse.ArgumentParser) -> None:
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--plan", action="store_true")
    mode.add_argument("--apply", action="store_true")
    mode.add_argument("--recover", action="store_true")
    mode.add_argument("--compact", action="store_true", help="Preserve all events in bounded immutable CAS storage")


def run(context: Context) -> Result:
    from unbake import migrate_state

    project = context.project()
    if context.args.compact:
        from unbake.work import attempts

        return Result.ok(NAME, attempts.compact(project), [], context.cmd("next"))
    if context.args.recover:
        return Result.ok(NAME, migrate_state.recover(project), [], context.cmd(NAME, "--plan"))
    planned = migrate_state.plan(project)
    result = planned if context.args.plan else migrate_state.apply(project, planned)
    return Result.ok(NAME, result, [], context.cmd(NAME, "--apply") if context.args.plan else context.cmd("next"))
