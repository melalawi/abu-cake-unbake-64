"""explain: show where a function stands and why."""

from __future__ import annotations

import argparse

from unbake.cli.args import Context
from unbake.cli.output import Result

NAME = "explain"
HELP = "Explain where FUNC or FILE stands: status, types, rodata, needs, order, similar."
DEFAULT_SECTIONS = ("status", "types", "rodata", "needs", "order", "similar")
SECTIONS = (*DEFAULT_SECTIONS, "subsystems", "hints")
DESCRIPTION = """\
Explain one function or draft file. Without --section, every section is shown.

  unbake explain func_80012345
  unbake explain build/work/func_80012345/func_80012345.c --section order

Sections: status (versions, sizes, best attempt, cycle eligibility before the size window),
types (the solved type context the draft uses),
rodata (which constants the function owns), needs (missing declarations and fields),
order (instruction order differences), similar (already matched functions that look alike).
Opt-in sections: subsystems (advisory memberships), hints (provider-aware source hypotheses).
"""
PROJECT = "ready"


def READ_ONLY(args: argparse.Namespace) -> bool:
    return True


def register(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("subject", metavar="FUNC|FILE")
    parser.add_argument("--section", choices=SECTIONS, action="append", help="Repeat to show several.")


def run(context: Context) -> Result:
    from unbake.work import explain

    sections = tuple(context.args.section or DEFAULT_SECTIONS)
    report = explain.explain(context.project(), context.require_host(), context.args.subject, sections)
    return Result.ok(
        NAME, report.document(), report.lines(), context.cmd(*report.next_words) if report.next_words else None
    )
