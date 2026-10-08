"""Explicit offline state migration, never entered by ordinary work."""

from __future__ import annotations

import argparse
from pathlib import Path

from unbake.cli.args import Context
from unbake.cli.output import Result

NAME = "migrate-state"
HELP = "Plan or apply the one-time preserved state migration."
DESCRIPTION = HELP
PROJECT = "raw"


def READ_ONLY(args: argparse.Namespace) -> bool:
    return False


def register(parser: argparse.ArgumentParser) -> None:
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--plan", action="store_true")
    mode.add_argument("--apply", type=Path, metavar="PLAN")
    mode.add_argument("--recover", action="store_true")
    mode.add_argument("--compact", action="store_true", help="Preserve all events in bounded immutable CAS storage")


def run(context: Context) -> Result:
    from unbake import migrate_state, strict_json
    from unbake.work.attempts import encoded

    assert context.root is not None
    if context.args.plan:
        planned = migrate_state.config_plan(context.root)
        from unbake import atomic

        path = context.root / ".unbake/migrations" / (planned["plan_digest"] + ".json")
        atomic.write(path, encoded(planned))
        return Result.ok(NAME, planned | {"plan_path": str(path)}, [], context.cmd(NAME, "--apply", path))
    if context.args.apply:
        result = migrate_state.apply_config(context.root, strict_json.read(context.args.apply))
        return Result.ok(NAME, result, [], context.cmd("next"))
    if context.args.recover and (context.root / "build/config-migration.journal" / "index.json").is_file():
        from unbake.journal import recover

        result = {
            "recovered_files": len(recover(context.root / "build/config-migration.journal", root=context.root)),
            "native_calls": 0,
        }
        return Result.ok(NAME, result, [], context.cmd(NAME, "--plan"))
    project = context.project()
    if context.args.compact:
        from unbake.work import attempts

        return Result.ok(NAME, attempts.compact(project), [], context.cmd("next"))
    if context.args.recover:
        return Result.ok(NAME, migrate_state.recover(project), [], context.cmd(NAME, "--plan"))

    raise AssertionError("migration operation required")
