"""cycle: one working round: pick functions, draft and compare in parallel, land each exact one at once."""

from __future__ import annotations

import argparse
import re
from dataclasses import asdict

from unbake.cli.args import Context, positive
from unbake.cli.output import Result
from unbake.config import Held
from unbake.process import named as cause_named

NAME = "cycle"
HELP = "Work a round: pick, draft, compare on every save, land each exact function at once."
DESCRIPTION = """\
Work one round on several functions at once. Each picked function gets a draft in
build/work/FUNC/FUNC.c. Every time you save that file it is compared again by itself. A function that
is exact in every version is landed (ROM proof and commit) within seconds, while the others keep going.
A published unit that breaks a source rule is a candidate too: its draft is its own src/ text, and once it is
exact and clean it lands again as "Clean UNIT".
At the end the attempt history is folded into attempts.json and committed as "Record attempts: FUNC, ..."
with the progress reports it moves (only when an attempt changed it), so fuzzy progress and the ranking
survive a fresh clone.

stdout carries one JSON event per line. With a terminal on stderr you also see a live board; without
one, choose the functions and the stop condition with flags.

  unbake cycle                                   # interactive: pick on screen, board on screen
  unbake cycle --pick 5 --stop idle:900          # take the 5 best candidates; stop after 15 idle minutes
  unbake cycle --functions func_a,func_b --stop all-landed
  unbake cycle --list                            # ranked candidates as JSON lines, then exit
  unbake cycle --status                          # the state of the running or last cycle

Stop conditions: all-landed, idle:SECONDS (no save or land for that long), after:MINUTES.

Events: every line has v, seq, t and event, one of cycle.start, fn.queued, fn.draft.start,
fn.draft.done, fn.edit, fn.compare.start, fn.compare.done (per-version percentages and the first
difference), fn.exact, fn.landed, fn.land_failed, fn.committed, cycle.committed (attempts or generated
files), fn.held, fn.failed, step.run (types, headers and build files brought current before the first
draft and after each land; drafts and compares that read the older tree are done again), steps.held (a step
refused: the cycle stops), fn.recheck (a landed function measured again after its land's steps) and
cycle.end (what landed and the next command).

Exit codes: 0 everything picked landed; 1 something was held or failed, is carried over, a step
refused (cycle.steps) or a land no longer matches after its steps (cycle.regressed); 130 interrupted.
"""
PROJECT = "ready"


def READ_ONLY(args: argparse.Namespace) -> bool:
    return bool(args.status or args.list)


def stop(value: str) -> str:
    if value == "all-landed" or re.fullmatch(r"(idle|after):[1-9]\d*", value):
        return value
    raise argparse.ArgumentTypeError(f"{value!r}: expected all-landed, idle:SECONDS or after:MINUTES")


def register(parser: argparse.ArgumentParser) -> None:
    choose = parser.add_mutually_exclusive_group()
    choose.add_argument("--pick", type=positive, metavar="N", help="Take the top N ranked candidates.")
    choose.add_argument("--functions", metavar="A,B,C", help="Take exactly these functions.")
    choose.add_argument("--list", action="store_true", help="Print ranked candidates as JSON lines and exit.")
    choose.add_argument("--status", action="store_true", help="Print the running or last cycle's state.")
    parser.add_argument("--stop", type=stop, metavar="CONDITION", help="all-landed, idle:SECONDS or after:MINUTES.")


def run(context: Context) -> Result:
    from unbake.cycle import engine

    args = context.args
    if args.status:
        return Result.ok(NAME, engine.status(context.project()), [], None)
    if args.list:
        rows = engine.ranked(context.project(), context.require_host())
        from unbake.layout import subsystems
        from unbake.work import plan

        snapshot = subsystems.snapshot(context.project())
        proposals = plan.cohorts(rows, snapshot)
        for row in rows:
            engine.emit_row(context.stdout, row)
        return Result.ok(
            NAME,
            {"candidates": len(rows), "cohorts": [asdict(row) for row in proposals]},
            [f"{len(rows)} candidates"],
            context.cmd("cycle", "--pick", "5", "--stop", "idle:900"),
        )
    interactive = engine.interactive()
    if not interactive and args.pick is None and args.functions is None:
        raise Held(
            cause_named(
                "cycle.pick",
                "cycle.pick: supply --pick N or --functions A,B,C (no terminal to choose on)",
                owner="cli.cycle",
                stage="cycle",
            )
        )
    if not interactive and args.stop is None:
        raise Held(
            cause_named(
                "cycle.stop",
                "cycle.stop: supply --stop all-landed|idle:SECONDS|after:MINUTES (no terminal)",
                owner="cli.cycle",
                stage="cycle",
            )
        )
    functions = tuple(name for name in (args.functions or "").split(",") if name)
    outcome = engine.run(
        context.project(),
        context.require_host(),
        pick=args.pick,
        functions=functions,
        stop=args.stop,
        events=context.stdout,
        next_words=lambda *words: context.cmd(*words),
    )
    return outcome
