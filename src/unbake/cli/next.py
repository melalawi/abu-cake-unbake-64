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
    parser.add_argument(
        "--blocked", action="store_true", help="Show dependency-bound refusals without ranking or native work."
    )
    parser.add_argument("--undrafted", action="store_true", help="Pick only functions with no draft.")


def run(context: Context) -> Result:
    if context.args.blocked:
        from unbake.work.attempts import ledger

        rows = []
        for outcome in ledger(context.project()).blocked():
            assert outcome.fault is not None
            cause = outcome.fault.cause
            deps = cause.dependency_set
            hashes = {
                **{p.path.name: p.sha256 for p in deps.files},
                **{"recipe:" + k: v for k, v in deps.recipes.items()},
            }
            from unbake import cache, inputs

            hashes.update(
                {
                    "value:" + k: inputs.bytes_digest(cache.serialized(v), algorithm="sha256")
                    for k, v in deps.values.items()
                }
            )
            from unbake.inputs import DependencySet, file_pin
            from unbake.work.attempts import dependency_changes

            host = context.require_host()
            pins = tuple(
                file_pin(
                    context.project().root.joinpath(*pin.path.parts),
                    root=context.project().root,
                    root_id=pin.path.root,
                    reuse=False,
                )
                if pin.path.root == "project"
                else pin
                for pin in deps.files
            )
            values = dict(deps.values)
            if "memory_worker_bytes" in values:
                values["memory_worker_bytes"] = host.memory_worker_bytes
            recipes = dict(deps.recipes)
            tool = __import__("pathlib").Path(__file__).parents[1]
            for name in recipes:
                path = tool / name
                if path.is_file():
                    recipes[name] = inputs.digest(path, algorithm="sha256", reuse=False)
            changes = dependency_changes(deps, DependencySet(pins, values, recipes))
            relevant = [k for k in changes if not cause.retry.watch or k in cause.retry.watch]
            rows.append(
                {
                    "cause_id": cause.id,
                    "key": cause.key,
                    "failing_stage": cause.stage,
                    "blocked_operation": cause.subject,
                    "source_hash": deps.values.get("source_sha256"),
                    "dependency_hashes": hashes,
                    "retry_allowed": cause.retryability == "unknown"
                    or (bool(relevant) and cause.retry.kind != "never"),
                    "changed_dependencies": relevant,
                    "resume_command": cause.action.render(context),
                    "required_change": list(cause.retry.watch),
                    "resource": {
                        "configured_cap_bytes": cause.evidence.get("configured_cap_bytes"),
                        "observed_bytes": cause.evidence.get("observed_bytes"),
                    },
                    "alternatives": [],
                }
            )
        rows.sort(key=lambda row: (row["failing_stage"], row["blocked_operation"], row["cause_id"]))
        return Result.ok(
            NAME, {"blocked": rows, "work": {"ranks": 0, "source_scans": 0, "solves": 0, "native_calls": 0}}, [], None
        )
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
