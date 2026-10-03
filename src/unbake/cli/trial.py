"""Compare an editable source using configured work and holding versions."""

import argparse
import shlex
from pathlib import Path

from unbake.cli.common import Subparsers, receipt, suggest
from unbake.decomp import checks, fuzzy_bar, trial
from unbake.decomp.commands import prefix
from unbake.decomp.trial_target import owning_versions
from unbake.match.batch import FOLDED_RULES
from unbake.project.config import Policy, Project, Unfinished, load_policy


def register(phases: Subparsers) -> None:
    parser = phases.add_parser("try", phase="try", help="Compile and retain exact current-input trial evidence.")
    parser.add_argument("source", type=Path, metavar="FILE")
    parser.add_argument("--scratch", type=Path, required=True, help="Private output directory outside the project.")
    parser.add_argument("--overlay-root", type=Path, help="Explicit include tree to stage privately.")
    parser.add_argument("--flags", action="store_true", help="Measure explicit compiler flag alternatives.")


def run(args: argparse.Namespace, project: Project, policy: Policy) -> bool:
    if args.source.suffix == ".h":
        raise Unfinished("try", "trial.struct")
    local = project.tools / "clone-policy.toml"
    if local.is_file():
        policy = load_policy(local)
    blockers = [
        finding for finding in checks.run(args.source) if finding.fakematch is None and finding.rule not in FOLDED_RULES
    ]
    if blockers:
        detail = "; ".join(f"{args.source}:{checks.message(finding)}" for finding in blockers)
        print("owner fuzzy bar: FAIL")
        print(f"HELD(try): trial.source_rules: {detail}")
        suggest(f"Edit {args.source}. Then run " + shlex.join([*prefix(project), "try", str(args.source)]))
        return True
    result = trial.retain_draft(
        project, policy, args.source, args.scratch, versions=None, flags=args.flags, overlay_root=args.overlay_root
    )
    if hasattr(result, "compares"):
        verdict = fuzzy_bar.evaluate(
            result.compares, owning_versions(project, result.function, None), result.preconditions
        )
        receipt("try", verdict.lines)
        if verdict.passed:
            suggest(result.next_command)
    return receipt("try", [f"retained {result.function} source_sha256 {result.source_sha256}"])
