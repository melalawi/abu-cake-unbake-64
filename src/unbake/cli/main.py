"""The unbake command line: parse, run one verb, print one JSON result (stdout) and human text (stderr)."""

from __future__ import annotations

import sys
from contextlib import nullcontext, redirect_stdout
from pathlib import Path
from types import ModuleType
from typing import TextIO

from unbake import config
from unbake.cli import (
    boundary,
    check,
    compare,
    cycle,
    draft,
    explain,
    guidance,
    init,
    publish,
    recompute,
    search_variants,
    setup,
    tidy,
)
from unbake.cli import next as next_verb
from unbake.cli.args import Context, HelpRequested, Parser
from unbake.cli.output import Result, emit
from unbake.config import Held

VERBS: tuple[ModuleType, ...] = (
    init,
    setup,
    next_verb,
    draft,
    compare,
    tidy,
    search_variants,
    publish,
    boundary,
    check,
    explain,
    cycle,
    recompute,
)
BY_NAME = {verb.NAME: verb for verb in VERBS}

DESCRIPTION = """\
Decompile N64 games one function at a time. Every command prints one JSON
result on stdout and human-readable text on stderr.

Typical session:
  unbake next                      # what to do now
  unbake draft FUNC                # writes build/work/FUNC/FUNC.c
  unbake compare build/work/FUNC/FUNC.c
  unbake publish build/work/FUNC/FUNC.c
Or let one command run the whole loop:
  unbake cycle --pick 5 --stop idle:900
"""


def make_parser() -> Parser:
    parser = Parser(prog="unbake", description=DESCRIPTION)
    parser.add_argument("--project", type=Path, metavar="DIR", help="Project root (default: found from cwd).")
    parser.add_argument("--config", type=Path, metavar="FILE", help="Host config file (default: unbake.toml).")
    verbs = parser.add_subparsers(dest="command", required=True, metavar="COMMAND", parser_class=Parser)
    for verb in VERBS:
        # argparse lists a subcommand in help whenever a help= keyword is given, even None.
        listed = {} if getattr(verb, "HIDDEN", False) else {"help": verb.HELP}
        sub = verbs.add_parser(
            verb.NAME, description=verb.DESCRIPTION, formatter_class=parser.formatter_class, **listed
        )
        verb.register(sub)
    return parser


def _root(verb: ModuleType, args: object) -> Path | None:
    if verb.PROJECT == "none":
        return None
    explicit = getattr(args, "project", None)
    return explicit.expanduser().resolve() if explicit is not None else config.discover()


def _run(argv: list[str] | None, stdout: TextIO) -> Result:
    parser = make_parser()
    try:
        args = parser.parse_args(argv)
    except HelpRequested as requested:
        return Result.ok("help", {"usage": requested.usage}, [], None)
    except Held as error:
        return Result.held("usage", error, "unbake --help")
    verb = BY_NAME[args.command]
    context = Context(verb.NAME, args, None, args.config.expanduser().absolute() if args.config else None, stdout)
    try:
        context.root = _root(verb, args)
        if config.NEEDS.get(verb.NAME):
            context.host = config.load_host(args.config, context.root, verb.NAME)
        writer = verb.PROJECT != "none" and not verb.READ_ONLY(args)
        from unbake import lock

        guard = lock.project_lock(context.root, verb.NAME) if writer and context.root else nullcontext()
        with guard:
            return verb.run(context)  # type: ignore[no-any-return]
    except Held as error:
        return Result.held(verb.NAME, error, error.next_action or guidance.after(context, error))
    except KeyboardInterrupt:
        return Result.held(verb.NAME, Held(verb.NAME, "interrupted: stopped by the user"), None)


def main(argv: list[str] | None = None) -> int:
    stdout = sys.stdout
    try:
        with redirect_stdout(sys.stderr):
            result = _run(argv, stdout)
    except Exception as error:
        result = Result.failed("unbake", error)
    code = emit(result, stdout, sys.stderr)
    return 130 if result.key == "interrupted" else code


if __name__ == "__main__":
    raise SystemExit(main())
