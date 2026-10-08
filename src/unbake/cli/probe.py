"""probe: measure a candidate source the way publish would and keep every artifact for inspection."""

from __future__ import annotations

import argparse
from pathlib import Path

from unbake.cli.args import Context
from unbake.cli.output import Result

NAME = "probe"
HELP = "Measure a candidate for FUNC exactly as publish would and write its artifacts to a directory."
DESCRIPTION = """\
Compile SOURCE as FUNC with the project's own recipe, link it through the same path compare and publish
use, and write into --out: candidate.s, candidate.o, linked.bin, original.bin, linked.dis, original.dis,
result.json (measurement, typed diffs, placement problems, source rules, volatile and landable findings,
exact). Exact means what publish requires for that version. Nothing in the project or its ledger changes
and several probes can run at once with different --out directories.

  unbake probe func_80012345 --source try.c --out /tmp/p1
  unbake probe func_80012345 --source try.c --out /tmp/p2 --flag=-O1 --omit-flag=-mips2 --flag=-dgreg

--flag adds a compiler flag (repeatable). Dump flags such as -dgreg or -dlreg are kept out of the
measurement and rerun beside it, their files land in OUT/dumps. --omit-flag drops a flag the project
recipe sets. --compiler picks another compiler id from the project's registry.
"""
PROJECT = "ready"


def READ_ONLY(args: argparse.Namespace) -> bool:
    return True


def register(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("function", metavar="FUNC")
    parser.add_argument("--source", type=Path, required=True, help="C file to measure (any name).")
    parser.add_argument("--out", type=Path, required=True, help="Directory for the artifacts (outside the project).")
    parser.add_argument("--version", help="Version to measure (default: the first holding version).")
    parser.add_argument("--flag", action="append", default=[], help="Extra compiler flag (repeatable).")
    parser.add_argument("--omit-flag", action="append", default=[], help="Drop this flag from the recipe.")
    parser.add_argument("--compiler", help="Compiler id from the project's registry.")


def run(context: Context) -> Result:
    from unbake import steps
    from unbake.work import probe

    project, host = context.project(), context.require_host()
    steps.ensure(project, host, ("buildfiles",))
    args = context.args
    data = probe.run(
        project,
        host,
        args.function,
        args.source,
        args.out,
        version=args.version,
        flags=tuple(args.flag),
        omit=tuple(args.omit_flag),
        compiler=args.compiler,
    )
    state = "EXACT" if data["exact"] else f"{data['identical_words']}/{data['target_words']} words identical"
    lines = [f"{data['function']} {data['version']}: {state}", f"artifacts: {args.out}/result.json"]
    lines += [f"constant: {p}" for p in data["placement_problems"]]
    lines += [f"rule broken: {row['sentence']}" for row in data["source_hygiene"]]
    return Result.ok(NAME, data, lines, None)
