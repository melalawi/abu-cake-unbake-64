"""The decomp command arguments and execution."""

from __future__ import annotations

import argparse
import json
import math
import shlex
from dataclasses import asdict
from pathlib import Path

from unbake.cli.common import Subparsers, count, receipt, suggest
from unbake.decomp.commands import prefix
from unbake.project.config import Held, Policy, Project


def register(phases: Subparsers) -> None:
    decomp = phases.add_parser("decomp", phase="decomp", help="Assign and retain NON_MATCHING drafts.")
    decomp_verbs = decomp.add_subparsers(dest="verb", required=True)
    decomp_verbs.add_parser("plan", phase="decomp")
    similar = decomp_verbs.add_parser("similar", phase="decomp")
    similar.add_argument("function")
    similar.add_argument("--version", metavar="V")
    similar.add_argument("--top-k", type=count, default=5)
    similar.add_argument("--bound", type=int, default=512)
    guide = decomp_verbs.add_parser(
        "guide", phase="decomp", help="Show prologue and declaration guidance for all or one VERSION."
    )
    guide.add_argument("function")
    guide.add_argument("--version", metavar="V")
    assign = decomp_verbs.add_parser("assign", phase="decomp")
    assign.add_argument("--holder", required=True)
    assign.add_argument("--tier", required=True)
    selection = assign.add_mutually_exclusive_group(required=True)
    selection.add_argument("--count", type=count)
    selection.add_argument("--function")
    release = decomp_verbs.add_parser("release", phase="decomp")
    release.add_argument("assignment_id")
    gbi = decomp_verbs.add_parser("gbi", phase="decomp", help="Rewrite proven Gfx word pairs as standard GBI macros.")
    gbi.add_argument("files", type=Path, nargs="*", metavar="FILE")
    gbi.add_argument("--all", action="store_true", dest="all_files")
    cleanup = decomp_verbs.add_parser(
        "cleanup", phase="decomp", help="Prepare editable C using shared types and staged headers."
    )
    cleanup.add_argument("source", type=Path, metavar="FILE")
    search = decomp_verbs.add_parser("search", phase="decomp")
    search.add_argument("source", type=Path, nargs="?", metavar="FILE")
    search.add_argument("--method", required=True, help="registers, order, permute; list prints available methods.")
    search.add_argument("--out", type=Path)
    search.add_argument("--budget-seconds", type=float)
    search.add_argument("--permute-version", metavar="V")
    search.add_argument("--permute-target", type=Path, metavar="OBJECT")
    search.add_argument("--permute-budget", type=float, metavar="SECONDS")
    explain = decomp_verbs.add_parser("explain", phase="decomp", help="Read compiler scheduling evidence.")
    explain.add_argument("kind", choices=("order",))
    explain.add_argument("source", type=Path, metavar="FILE")
    explain.add_argument("--version", required=True, metavar="V")
    best = decomp_verbs.add_parser("best", phase="decomp")
    best.add_argument("function")
    publish = decomp_verbs.add_parser("publish", phase="decomp")
    publish.add_argument("--all", required=True, action="store_true")


def run(args: argparse.Namespace, project: Project, policy: Policy) -> None:
    if args.verb in ("assign", "release"):
        from unbake.decomp.assign import Ledger

        if args.verb == "assign":
            if args.count is not None:
                from unbake.decomp import plan

                rows = plan.assign(project, policy, args.holder, args.tier, count=args.count)
            else:
                rows = Ledger(project, policy).assign(args.holder, args.tier, function=args.function)
            receipt("decomp", [json.dumps(row, sort_keys=True) for row in rows])
        else:
            Ledger(project, policy).release(args.assignment_id)
            receipt("decomp", [f"released {args.assignment_id}"])
    elif args.verb == "plan":
        from unbake.decomp import plan

        ranked_rows = plan.ranked(project, policy)
        receipt("decomp", [json.dumps(asdict(row), sort_keys=True, default=str) for row in ranked_rows])
    elif args.verb == "similar":
        from unbake.decomp import similar

        examples = similar.retrieve(
            project, args.function, args.version or project.names_from, top_k=args.top_k, bound=args.bound
        )
        receipt("decomp", [json.dumps(asdict(row), sort_keys=True, default=str) for row in examples])
    elif args.verb == "guide":
        from unbake.decomp import guide

        print(guide.run(project, args.function, args.version))
    elif args.verb == "gbi":
        from unbake.decomp import gbi

        print(json.dumps(gbi.rewrite(project, policy, args.files, all_files=args.all_files), indent=2))
    elif args.verb == "cleanup":
        from unbake.decomp.cleanup import prepare
        from unbake.match import reporting

        with reporting.stream(lambda line: print(line, flush=True)):
            source = prepare(project, policy, args.source)
        suggest(shlex.join([*prefix(project), "try", str(source)]))
        receipt("decomp", [f"editable source prepared: {source}; headers remain staged"])
    elif args.verb == "search":
        from unbake.search import available, methods
        from unbake.search.core import run as search_run

        if args.method == "list":
            receipt("search", list(available()))
            return
        for name, value in (("source", args.source), ("--out", args.out), ("--budget-seconds", args.budget_seconds)):
            if value is None:
                raise Held("search", f"{name}: missing value for method {args.method}")
        selected = args.method.split(",")
        permute_values = (args.permute_version, args.permute_target, args.permute_budget)
        if "permute" in selected:
            for flag, value in zip(
                ("--permute-version", "--permute-target", "--permute-budget"), permute_values, strict=True
            ):
                if value is None:
                    raise Held("search", f"{flag}: missing value for method permute")
            if args.permute_version not in project.versions:
                raise Held("search", f"--permute-version {args.permute_version}: unknown VERSION")
            if not args.permute_target.is_file():
                raise Held("search", f"--permute-target {args.permute_target}: missing object")
            if not math.isfinite(args.permute_budget) or args.permute_budget <= 0:
                raise Held("search", "--permute-budget: expected positive finite seconds")
            from unbake.search.permute import Permuter

            generators = [
                Permuter(args.permute_version, args.permute_target, args.permute_budget)
                if name == "permute"
                else methods(name)[0]
                for name in selected
            ]
        else:
            if any(value is not None for value in permute_values):
                raise Held("search", "--permute-version/--permute-target/--permute-budget: require method permute")
            generators = methods(args.method)
        search_run(project, policy, args.source, generators, args.out, args.budget_seconds)
    elif args.verb == "explain":
        from unbake.decomp import explain

        print(json.dumps(asdict(explain.order(project, policy, args.source, args.version)), indent=2))
    else:
        from unbake.decomp.drafts import Store

        store = Store(policy, project)
        if args.verb == "best":
            best = store.best(args.function)
            if best is None:
                raise Held("decomp", f"no draft recorded for function {args.function}")
            receipt("decomp", [best])
        else:
            paths = store.publish_all()
            receipt("decomp", paths if paths else ["no drafts to publish"])
