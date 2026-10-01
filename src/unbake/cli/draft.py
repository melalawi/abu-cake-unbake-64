"""Create editable function work in the project's declared drafts subtree."""

import argparse
import re
import shlex
import shutil

from unbake.cli.common import Subparsers, receipt, suggest
from unbake.cli.guidance import command
from unbake.decomp import m2c, work
from unbake.decomp.trial_target import inputs, owning_versions
from unbake.project.config import Held, Policy, Project, Unfinished


def register(phases: Subparsers) -> None:
    parser = phases.add_parser("draft", phase="draft", help="Draft a function using the configured naming version.")
    parser.add_argument("function", nargs="?", metavar="FUNCTION")
    parser.add_argument("--struct", metavar="ID", help="Draft an evidenced shared struct (implementation pending).")


def run(args: argparse.Namespace, project: Project, policy: Policy) -> bool:
    if args.struct is not None:
        if args.function is not None:
            raise Held("draft", "draft.subject: select FUNCTION or --struct ID")
        raise Unfinished("draft", "draft.struct")
    function = args.function
    if function is None:
        raise Held("draft", "draft.function: supply FUNCTION")
    if not re.fullmatch(r"[A-Za-z_]\w*", function):
        raise Held("draft", "draft.function: expected a C identifier")
    versions = owning_versions(project, function, None)
    if project.names_from not in versions:
        raise Held("draft", f"project.names_from: {function} has no owner in {project.names_from}")
    destination = project.drafts / function
    source = destination / (function + ".c")
    if source.exists():
        raise Held("draft", f"draft.source: {source} already exists; edit and try it")
    with inputs(project, function, versions) as pinned:
        generated = m2c.draft(
            project, policy, function, project.names_from, project.work, generation=pinned[project.names_from][0]
        )
        destination.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(generated, source)
        shutil.copytree(generated.parent / "overlay", destination / "overlay")
        shutil.copyfile(generated.parent / "overlay.json", destination / "overlay.json")
        work.persist(project, work.identity(project, source, versions, pinned=pinned))
    suggest(command(project.root, "try") + " " + shlex.quote(str(source)))
    return receipt(
        "draft", [f"draft_path: {source}", f"versions: {', '.join(versions)}; names_from: {project.names_from}"]
    )
