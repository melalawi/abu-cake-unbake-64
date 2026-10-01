"""The unbake command tree and phase dispatch."""

import argparse
import os
import shlex
import sys
from contextlib import suppress
from pathlib import Path

from unbake.cli import (
    check,
    clone,
    common,
    decomp,
    draft,
    guidance,
    init,
    match,
    report,
    rodata,
    setup,
    split,
    submit,
    trial,
)
from unbake.cli import (
    next as next_command,
)
from unbake.cli.common import Parser
from unbake.project import config
from unbake.project.config import Held, Policy, Project


def make_parser() -> argparse.ArgumentParser:
    parser = Parser(prog="unbake", description="Build and match N64 decompilation projects.")
    parser.add_argument("--project", type=Path, metavar="DIR")
    parser.add_argument("--policy", type=Path, metavar="FILE")
    phases = parser.add_subparsers(dest="phase", required=True)
    for command in (setup, init, split, decomp, match, report, check, clone, rodata, draft, trial, submit):
        command.register(phases)
    phases.add_parser("next", phase="next", help="Show the next required project action.")
    return parser


def dispatch(args: argparse.Namespace, project: Project, policy: Policy) -> bool:
    if args.phase == "decomp":
        decomp.run(args, project, policy)
        return False
    commands = {
        "draft": draft.run,
        "try": trial.run,
        "submit": submit.run,
        "rodata": rodata.run,
        "clone": clone.run,
        "split": split.run,
        "match": match.run,
        "report": report.run,
        "check": check.run,
    }
    return commands[args.phase](args, project, policy)


def main(argv: list[str] | None = None) -> int:
    phase = "config"
    root = None
    missing = None
    tokens = sys.argv[1:] if argv is None else argv
    json_output = any(
        tokens[index : index + 2] in (["rodata", "owners"], ["decomp", "gbi"]) for index in range(len(tokens))
    )
    retry = "unbake setup"
    invocation = common.begin()
    try:
        args = make_parser().parse_args(argv)
        phase = args.phase
        json_output = (phase == "rodata" and args.verb == "owners") or (phase == "decomp" and args.verb == "gbi")
        retry = "unbake " + phase
        if args.policy is not None:
            os.environ["UNBAKE_POLICY"] = str(args.policy.expanduser().absolute())
        if phase == "init":
            refused = init.run(args)
            root = args.target.expanduser().absolute()
            return int(refused or invocation.refused)
        if phase == "clone":
            clone.validate(args)
        root = args.project.expanduser().resolve() if args.project is not None else config.discover()
        retry = guidance.command(root, phase)
        if args.policy is not None:
            tokens = shlex.split(retry)
            retry = shlex.join([*tokens[:-1], "--policy", str(args.policy.expanduser().absolute()), tokens[-1]])
        if phase == "next":
            return int(next_command.run(args, config.load_pending(root)) or invocation.refused)
        if phase == "setup":
            return int(setup.run(args, config.load_pending(root)) or invocation.refused)
        project = config.load(root)
        policy = config.load_policy()
        refused = dispatch(args, project, policy)
        return int(refused or invocation.refused)
    except Held as error:
        print(f"HELD({error.phase}): {error.reason}", file=sys.stderr if json_output else sys.stdout)
        if phase == "config":
            retry = "unbake --help" if error.phase == "config" else f"unbake {error.phase}"
        missing = error.reason.split(":", 1)[0]
        return 1
    except (ImportError, OSError, RuntimeError, ValueError) as error:
        print(f"HELD({phase}): {error}", file=sys.stderr if json_output else sys.stdout)
        missing = str(error).split(":", 1)[0]
        return 1
    except KeyboardInterrupt:
        print(f"HELD({phase}): interrupted", file=sys.stderr if json_output else sys.stdout)
        missing = "interrupted operation"
        return 130
    finally:
        if root is None:
            with suppress(Held):
                root = config.discover()
        missing = missing or invocation.missing_input
        if missing is not None:
            action = guidance.resolve(root, missing=missing, retry=retry)
        else:
            action = invocation.next_action or guidance.resolve(root, retry=retry)
        common.finish(action, json_output=json_output)


if __name__ == "__main__":
    raise SystemExit(main())
