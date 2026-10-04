"""next: say what to do now, as one runnable command."""

from __future__ import annotations

import argparse

from unbake.cli.args import Context
from unbake.cli.output import Result

NAME = "next"
HELP = "Print the next action as a runnable command."
DESCRIPTION = """\
Print the single next action for this project, with the reason.

  unbake next                # continue existing work first, then the best new function
  unbake next --undrafted    # only functions without a draft yet
"""
PROJECT = "pending"


def READ_ONLY(args: argparse.Namespace) -> bool:
    return True


def register(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--undrafted", action="store_true", help="Pick only functions with no draft.")


def run(context: Context) -> Result:
    pending = context.pending()
    if pending.state != "ready":
        if any(pending.roms.iterdir()) if pending.roms.is_dir() else False:
            return Result.ok(NAME, {"reason": "setup has not run"}, ["setup has not run"], context.cmd("setup"))
        reason = f"put the ROM files in {pending.roms}, then run `{context.cmd('setup')}`"
        return Result.ok(NAME, {"reason": reason}, [reason], f"stop: {reason}")
    from unbake.work import plan

    action = plan.next_action(context.project(), context.require_host(), undrafted=context.args.undrafted)
    following = context.cmd(*action.words) if action.words is not None else f"stop: {action.reason}"
    data = {"function": action.function, "reason": action.reason}
    return Result.ok(NAME, data, [action.reason], following)
