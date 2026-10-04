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
    collect,
    common,
    decomp,
    guidance,
    init,
    layout,
    report,
    rodata,
    setup,
    split,
)
from unbake.cli import (
    next as next_command,
)
from unbake.cli.common import Parser
from unbake.project import config
from unbake.project.config import Held, Policy, Project


def make_parser() -> argparse.ArgumentParser:
    from unbake.cli import draft, solve, submit, trial
    from unbake.cli import map as map_command

    parser = Parser(prog="unbake", description="Build and match N64 decompilation projects.")
    parser.add_argument("--project", type=Path, metavar="DIR")
    parser.add_argument("--policy", type=Path, metavar="FILE")
    phases = parser.add_subparsers(dest="phase", required=True)
    for command in (
        setup,
        init,
        layout,
        split,
        decomp,
        report,
        check,
        clone,
        collect,
        rodata,
        draft,
        trial,
        submit,
        map_command,
        solve,
    ):
        command.register(phases)
    next_parser = phases.add_parser("next", phase="next", help="Show the next required project action.")
    next_parser.add_argument(
        "--new", action="store_true", help="Skip existing drafts and select the best undrafted function."
    )
    next_parser.add_argument("--exclude", type=Path, metavar="FILE", help="Override the project exclusion manifest.")
    return parser


def dispatch(args: argparse.Namespace, project: Project, policy: Policy) -> bool:
    from unbake.cli import draft, solve, submit, trial
    from unbake.cli import map as map_command

    if args.phase == "decomp":
        decomp.run(args, project, policy)
        return False
    commands = {
        "draft": draft.run,
        "try": trial.run,
        "submit": submit.run,
        "map": map_command.run,
        "solve": solve.run,
        "layout": layout.run,
        "rodata": rodata.run,
        "clone": clone.run,
        "collect": collect.run,
        "split": split.run,
        "report": report.run,
        "check": check.run,
    }
    return commands[args.phase](args, project, policy)


def _contextualize(action: str, root: Path | None, policy: Path | None) -> str:
    if "; to redraft: " in action:
        selected, redraft = action.split("; to redraft: ", 1)
        return _contextualize(selected, root, policy) + "; to redraft: " + _contextualize(redraft, root, policy)
    if not action.startswith("unbake "):
        return action
    tokens = shlex.split(action)
    overrides = shlex.split(guidance.command(root, "next"))[1:-1] if "--project" not in tokens else []
    if policy is not None and "--policy" not in tokens:
        overrides += ["--policy", str(policy)]
    return shlex.join([tokens[0], *overrides, *tokens[1:]])


def main(argv: list[str] | None = None) -> int:
    phase = "config"
    root = None
    policy_override = None
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
            policy_override = args.policy.expanduser().absolute()
            os.environ["UNBAKE_POLICY"] = str(policy_override)
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
            pending = config.load_pending(root)
            if pending.state == "ready":
                config.read_policy(args.policy)
            return int(next_command.run(args, pending) or invocation.refused)
        if phase == "setup":
            return int(setup.run(args, config.load_pending(root)) or invocation.refused)
        project = config.load(root, persist_workspace=phase != "clone")
        policy = config.load_policy()
        refused = dispatch(args, project, policy)
        if phase == "clone" and not refused and not invocation.refused:
            root = args.destination.expanduser().resolve()
            retry = guidance.command(root, "next")
        return int(refused or invocation.refused)
    except Held as error:
        print(f"HELD({error.phase}): {error.reason}", file=sys.stderr if json_output else sys.stdout)
        if phase == "config":
            retry = "unbake --help" if error.phase == "config" else f"unbake {error.phase}"
        if error.next_action is not None:
            common.suggest(error.next_action, on_refusal=True)
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
            with suppress(Held, OSError, RuntimeError):
                root = config.discover()
        missing = missing or invocation.missing_input
        try:
            if missing is None and root is not None and not config.policy_path().is_file():
                with suppress(Held):
                    if config.load_pending(root).state == "ready":
                        missing = "policy.path"
            if invocation.next_on_refusal and invocation.next_action is not None:
                action = invocation.next_action
            elif missing is not None:
                action = guidance.resolve(root, missing=missing, retry=retry)
            else:
                action = invocation.next_action or guidance.resolve(root, retry=retry)
            action = _contextualize(action, root, policy_override)
        except (Held, ImportError, OSError, RuntimeError, ValueError, TypeError, AttributeError, KeyError):
            action = shlex.join(["unbake", "--project", str(root), "next"]) if root is not None else "unbake --help"
        common.finish(action, json_output=json_output)


if __name__ == "__main__":
    raise SystemExit(main())
