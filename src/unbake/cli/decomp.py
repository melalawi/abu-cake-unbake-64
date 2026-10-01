"""The decomp command arguments and execution."""

from __future__ import annotations

import argparse
import json
import math
import tempfile
from dataclasses import asdict
from pathlib import Path
from typing import TYPE_CHECKING

from unbake.cli.common import Subparsers, count, receipt

if TYPE_CHECKING:
    from unbake.decomp.trial import Trial
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
    guide = decomp_verbs.add_parser("guide", phase="decomp")
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
    draft = decomp_verbs.add_parser("draft", phase="decomp")
    draft.add_argument("function")
    draft.add_argument("--version", required=True, metavar="V")
    draft.add_argument("--scratch", type=Path, required=True, metavar="DIR")
    trial = decomp_verbs.add_parser("try", phase="decomp")
    trial.add_argument("source", type=Path, metavar="FILE")
    trial.add_argument("--scratch", type=Path, required=True, metavar="DIR")
    trial.add_argument("--version", action="append", metavar="V", help="Repeat to select VERSIONs.")
    search = decomp_verbs.add_parser("search", phase="decomp")
    search.add_argument("source", type=Path, metavar="FILE")
    search.add_argument("--method", required=True)
    search.add_argument("--out", type=Path, required=True)
    search.add_argument("--budget-seconds", type=float, required=True)
    search.add_argument("--permute-version", metavar="V")
    search.add_argument("--permute-target", type=Path, metavar="OBJECT")
    search.add_argument("--permute-budget", type=float, metavar="SECONDS")
    best = decomp_verbs.add_parser("best", phase="decomp")
    best.add_argument("function")
    publish = decomp_verbs.add_parser("publish", phase="decomp")
    publish.add_argument("--all", required=True, action="store_true")


def trial(args: argparse.Namespace, project: Project, policy: Policy, source: Path, versions: list[str] | None) -> None:
    from unbake.decomp import trial as draft_trial

    scratch = args.scratch.expanduser().resolve()
    if scratch.is_relative_to(project.root.resolve()):
        raise Held("decomp", f"scratch {scratch} is inside project.root {project.root}")
    scratch.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix=f"{source.stem}-", dir=scratch))
    from unbake.project.build import lock

    with lock(project):
        generations = {v: project.build_link(v).resolve() for v in project.versions}
        result = draft_trial.try_draft(project, policy, source, work, versions=versions)
        store_trial(project, policy, source, work, result, generations)
    receipt("decomp", [f"retained NON_MATCHING draft {result.function} {result.source_sha256}"])


def store_trial(
    project: Project, policy: Policy, source: Path, scratch: Path, result: Trial, generations: dict[str, Path]
) -> None:
    from unbake.decomp import drafts, score
    from unbake.report import progress as report

    scores = {}
    for v in result.compares:
        if project.build_link(v).resolve() != generations[v]:
            raise Held("decomp", f"build/{v}: generation changed during trial, retry using --scratch {scratch}")
        candidates = [path for path in scratch.rglob("*.elf") if path.parent.name == v]
        if len(candidates) != 1:
            raise Held(
                "decomp",
                f"VERSION {v} draft ELF in scratch {scratch}: expected exactly one *.elf, found {len(candidates)}",
            )
        candidate = candidates[0]
        objects = []
        for name in ("baserom", "draft"):
            binary = candidate.parent / (name + ".bin")
            try:
                content = binary.read_bytes()
            except OSError as error:
                raise Held("decomp", f"VERSION {v} trial.{name} bytes {binary}: {error}") from error
            obj = candidate.parent / (name + ".score.o")
            obj.write_bytes(report.target_object(result.function, content))
            objects.append(obj)
        scores[v] = score.fuzzy(project, policy, v, result.function, objects[0], objects[1])
        print(f"score VERSION {v}: objdiff {scores[v]:.6f}")
    drafts.Store(policy, project).add(result, source, scores)


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
    elif args.verb == "draft":
        from unbake.decomp import m2c
        from unbake.decomp.trial_compile import run_tool
        from unbake.project.build import lock

        with lock(project):
            generation = project.build_link(args.version)
            if not generation.is_symlink() or not (generation / f"{project.name}.elf").is_file():
                print(run_tool(["make", f"VERSION={args.version}", f"-j{policy.cores}"], project.root, "decomp"))
        source = m2c.draft(project, policy, args.function, args.version, args.scratch)
        trial(args, project, policy, source, None)
    elif args.verb == "search":
        from unbake.search import methods
        from unbake.search.core import run as search_run

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
    elif args.verb == "try":
        trial(args, project, policy, args.source, args.version)
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
