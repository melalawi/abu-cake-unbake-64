"""The unbake command line: parse, run one verb, print one JSON result (stdout) and human text (stderr)."""

from __future__ import annotations

import sys
from contextlib import nullcontext
from dataclasses import replace
from pathlib import Path
from types import ModuleType
from typing import TextIO

from unbake import admission, cache, config
from unbake.cli import (
    boundary,
    check,
    compare,
    cycle,
    draft,
    explain,
    guidance,
    init,
    migrate_state,
    publish,
    recompute,
    resources,
    search_variants,
    setup,
    tidy,
)
from unbake.cli import next as next_verb
from unbake.cli.args import Context, HelpRequested, Parser
from unbake.cli.output import Result, emit
from unbake.config import Held
from unbake.process import capture
from unbake.process import named as cause_named

VERBS: tuple[ModuleType, ...] = (
    init,
    migrate_state,
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
    resources,
)
BY_NAME = {verb.NAME: verb for verb in VERBS}

DESCRIPTION = """\
Decompile N64 games one function at a time. Every command prints one JSON
result on stdout and human-readable text on stderr.

Host config (--config FILE, $UNBAKE_CONFIG or ~/.config/unbake/unbake.toml)
must set [resources].domain = "standalone" to run alone, or an absolute broker
manifest path to share an allocation. Cores, workers and memory bound each command.

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
    parser.add_argument(
        "--config",
        type=Path,
        metavar="FILE",
        help="Host config file (otherwise $UNBAKE_CONFIG or ~/.config/unbake/unbake.toml).",
    )
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
            cache.configure(memory_bytes=context.host.cache_memory_bytes)
        writer = verb.PROJECT != "none" and not verb.READ_ONLY(args)
        from unbake import lock

        guard = lock.project_lock(context.root, verb.NAME) if writer and context.root else nullcontext()
        # Admission precedes project mutation locks and project preparation.
        from unbake.work.attempts import command_ledger

        history = command_ledger(context.project()) if verb.PROJECT == "ready" else nullcontext()
        with admission.command(context.host), guard, history:
            return verb.run(context).render_actions(context)  # type: ignore[no-any-return]
    except Held as error:
        data = {"failures": list(error.failures)} if error.failures else None
        return Result.held(verb.NAME, error, guidance.after(context, error), data)
    except Exception as error:  # nothing unexpected reaches the terminal raw: it is a refusal with a key and a Next
        from unbake.process import capture
        from unbake.process import named as cause_named

        unexpected = Held(
            capture(
                error,
                cause=cause_named(
                    f"{verb.NAME}.unexpected",
                    f"{verb.NAME}.unexpected: {_where(error)}",
                    owner="cli.main",
                    stage=verb.NAME,
                ),
            )
        )
        return Result.held(verb.NAME, unexpected, guidance.after(context, unexpected))
    except KeyboardInterrupt:
        words = argv if argv is not None else sys.argv[1:]
        following = (
            context.cmd(verb.NAME, *words[words.index(verb.NAME) + 1 :])
            if verb.NAME in words
            else context.cmd(verb.NAME)
        )
        return Result.interrupted(verb.NAME, following)


def _where(error: BaseException) -> str:
    """The error and the innermost tool frame that raised it."""
    import traceback

    frames = [frame for frame in traceback.extract_tb(error.__traceback__) if "/unbake/" in frame.filename]
    place = f" (at {Path(frames[-1].filename).name}:{frames[-1].lineno})" if frames else ""
    return f"{type(error).__name__}: {error}{place}"


def main(argv: list[str] | None = None) -> int:
    from unbake import effort, tui

    stdout = sys.stdout
    admission.receipt.clear()
    started = effort.mark()
    tui.start(tui.stderr(), tui.interactive())
    try:
        result = _run(argv, stdout)
    except Exception as error:
        result = Result.held(
            "unbake",
            Held(
                capture(
                    error,
                    cause=cause_named(
                        "unbake.unexpected", f"unbake.unexpected: {_where(error)}", owner="cli.main", stage="unbake"
                    ),
                )
            ),
            "unbake next",
        )
    finally:
        tui.stop()
    # Every result says what the command cost: wall, CPU of this process, its tools and its pool work by function.
    spent = effort.since(started)
    measured = spent.document()
    if admission.receipt:
        measured["admission"] = dict(admission.receipt)
    result = replace(result, effort=measured)
    code = emit(result, stdout, tui.stderr())
    # The slowest stages, so no run is blind about where time went or whether it ran in parallel.
    lines = effort.summary(spent.stages)
    if lines:
        print("Slowest stages:", *lines, sep="\n  ", file=tui.stderr())
    return code


if __name__ == "__main__":
    raise SystemExit(main())
