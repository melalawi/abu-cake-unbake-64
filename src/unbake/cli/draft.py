"""Create editable function work in the project's declared drafts subtree."""

import argparse
import re
import shlex
import shutil
from uuid import uuid4

from unbake.cli.common import Subparsers, receipt, suggest
from unbake.cli.guidance import command
from unbake.decomp import m2c, type_context, work
from unbake.decomp.trial_target import inputs, owning_versions
from unbake.project.config import Held, Policy, Project, Unfinished, load_policy


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
    local = project.tools / "clone-policy.toml"
    if local.is_file():
        policy = load_policy(local)
    versions = owning_versions(project, function, None)
    naming = versions[0]
    database, context = type_context.required(project)
    destination = project.drafts / function
    source = destination / (function + ".c")
    refresh = source.exists() and function in type_context.redrafts(project)
    if source.exists() and not refresh:
        raise Held("draft", f"draft.source: {source} already exists; edit and try it")
    with inputs(project, function, versions) as pinned:
        generated = m2c.draft(
            project,
            policy,
            function,
            naming,
            project.work,
            generation=pinned[naming][0],
            type_context=context,
            announce=False,
        )
        if refresh:
            archive = project.work / (function + ".redraft." + uuid4().hex)
            shutil.move(str(destination), archive)
        destination.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(generated, source)
        shutil.copytree(generated.parent / "overlay", destination / "overlay")
        shutil.copyfile(generated.parent / "overlay.json", destination / "overlay.json")
        work.persist(project, work.identity(project, source, versions, pinned=pinned, policy=policy))
    type_context.clear_redraft(project, function, database)
    suggest(command(project.root, "try") + " " + shlex.quote(str(source)))
    return receipt("draft", [f"draft_path: {source}", f"versions: {', '.join(versions)}; draft version: {naming}"])
