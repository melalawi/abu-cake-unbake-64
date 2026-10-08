"""boundary: edit where functions and data start and end, in layout.toml and the version splits."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from unbake.cli.args import Context, integer
from unbake.cli.output import Result
from unbake.config import Held
from unbake.process import named as cause_named

NAME = "boundary"
HELP = "Edit function and data boundaries (preview by default; --apply to write)."
DESCRIPTION = """\
Edit where functions and data start and end. Every form previews the change; add --apply to write it
(an applied change is proved by rebuilding the affected versions).

  unbake boundary function FUNC --version V --start 0x1000 --end 0x1040 [--apply]
  unbake boundary data NAME --version V --start 0x2000 --end 0x2010 [--apply]
  unbake boundary code-in-data FUNC --version V --start 0x3000 --end 0x3080 [--apply]
  unbake boundary import FILE [--apply]          # JSON list of boundary edits
  unbake boundary prelude [FUNC ...] [--version V ...] [--apply]   # split dead leading bytes off a function
  unbake boundary split [FUNC ...] [--version V ...] [--apply]     # cut a unit that holds two functions
  unbake boundary merge [FUNC ...] [--version V ...] [--apply]     # join fragments back into their function
  unbake boundary same-symbol FILE [--apply]     # assert placements in several versions are one symbol
  unbake boundary name-data NAME --version V --address 0x80100000 [--rename-from OLD] [--apply]
"""
PROJECT = "ready"


def READ_ONLY(args: argparse.Namespace) -> bool:
    return not args.apply


def register(parser: argparse.ArgumentParser) -> None:
    verbs = parser.add_subparsers(dest="verb", required=True, metavar="VERB", parser_class=type(parser))
    for name, subject in (("function", "FUNC"), ("data", "NAME"), ("code-in-data", "FUNC")):
        verb = verbs.add_parser(name, help=f"Mark a ROM byte range as {name.replace('-', ' ')}.")
        verb.add_argument("subject", metavar=subject)
        verb.add_argument("--version", required=True, metavar="V")
        verb.add_argument("--start", type=integer, required=True, help="Inclusive ROM offset.")
        verb.add_argument("--end", type=integer, required=True, help="Exclusive ROM offset.")
        verb.add_argument("--apply", action="store_true")
    imported = verbs.add_parser("import", help="Apply a JSON file of boundary edits.")
    imported.add_argument("file", type=Path, metavar="FILE")
    imported.add_argument("--apply", action="store_true")
    lead = verbs.add_parser("prelude", help="Split dead leading bytes, jump thunks and pre-frame stubs.")
    lead.add_argument("subject", nargs="*", metavar="FUNC")
    lead.add_argument("--version", action="append", default=[], metavar="V")
    lead.add_argument("--apply", action="store_true")
    cut = verbs.add_parser("split", help="Cut a unit that holds several functions where one ends and the next begins.")
    cut.add_argument("subject", nargs="*", metavar="FUNC")
    cut.add_argument("--version", action="append", default=[], metavar="V")
    cut.add_argument("--apply", action="store_true")
    joined = verbs.add_parser("merge", help="Join fragments a split left behind back into their function.")
    joined.add_argument("subject", nargs="*", metavar="FUNC")
    joined.add_argument("--version", action="append", default=[], metavar="V")
    joined.add_argument("--apply", action="store_true")
    same = verbs.add_parser("same-symbol", help="Assert placements in several versions are one symbol.")
    same.add_argument("file", type=Path, metavar="FILE")
    same.add_argument("--apply", action="store_true")
    named = verbs.add_parser("name-data", help="Name or rename a data symbol.")
    named.add_argument("subject", metavar="NAME")
    named.add_argument("--version", metavar="V")
    named.add_argument("--address", type=integer)
    named.add_argument("--rename-from", metavar="OLD")
    named.add_argument("--all-versions", action="store_true", help="Infer placements in every version from code.")
    named.add_argument("--apply", action="store_true")


def run(context: Context) -> Result:
    from unbake import steps
    from unbake.layout import boundary_ops

    args = context.args
    project, host = context.project(), context.require_host()
    if args.apply:
        words = ["boundary", args.verb]
        if args.verb in ("import", "same-symbol"):
            words.append(str(args.file))
        elif args.verb in ("prelude", "split", "merge"):
            words.extend(args.subject)
            for version in args.version:
                words.extend(("--version", version))
        else:
            words.append(args.subject)
            for option in ("version", "start", "end", "address", "rename_from"):
                value = getattr(args, option, None)
                if value is not None:
                    words.extend(("--" + option.replace("_", "-"), str(value)))
            if getattr(args, "all_versions", False):
                words.append("--all-versions")
        words.append("--apply")
        prepared = steps.prepare(
            project, host, steps.PrepareRequest("boundary", (), project_scope=True, resume_command=context.cmd(*words))
        )
        prepared.assert_current(project)
    if args.verb == "import":
        outcome = boundary_ops.import_file(project, host, args.file, apply=args.apply)
    elif args.verb == "prelude":
        outcome = boundary_ops.prelude(project, host, args.subject, args.version, apply=args.apply)
    elif args.verb == "split":
        outcome = boundary_ops.functions(project, host, args.subject, args.version, apply=args.apply)
    elif args.verb == "merge":
        outcome = boundary_ops.merge(project, host, args.subject, args.version, apply=args.apply)
    elif args.verb == "same-symbol":
        outcome = boundary_ops.same_symbol(project, host, args.file, apply=args.apply)
    elif args.verb == "name-data":
        if args.all_versions and (args.version or args.address is not None or args.rename_from):
            raise Held(
                cause_named(
                    "boundary.name_data",
                    "boundary.name_data: --all-versions excludes --version, --address and --rename-from",
                    owner="cli.boundary",
                    stage="boundary",
                )
            )
        if not args.all_versions and (args.version is None or args.address is None):
            raise Held(
                cause_named(
                    "boundary.name_data",
                    "boundary.name_data: supply --version and --address, or --all-versions",
                    owner="cli.boundary",
                    stage="boundary",
                )
            )
        outcome = boundary_ops.name_data(
            project,
            host,
            args.subject,
            args.version,
            args.address,
            args.rename_from,
            args.all_versions,
            apply=args.apply,
        )
    else:
        outcome = boundary_ops.interval(
            project, host, args.verb, args.subject, args.version, args.start, args.end, apply=args.apply
        )
    data = {"applied": args.apply, "diff": outcome.diff, "versions": outcome.versions}
    if not args.apply:
        return Result.ok(NAME, data, [*outcome.lines, json.dumps({"preview_edits": len(outcome.diff)})], None)
    return Result.ok(NAME, data, outcome.lines, context.cmd("next"))
