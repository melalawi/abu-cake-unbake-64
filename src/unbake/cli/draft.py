"""Create editable function work in the project's declared drafts subtree."""

import argparse
import re
import shlex
import shutil
from pathlib import Path
from uuid import uuid4

from unbake.cli.common import Subparsers, receipt, suggest
from unbake.cli.guidance import command
from unbake.decomp import draft_presence, exclusions, m2c, type_context, work
from unbake.decomp.trial_compile import default_scratch, scratch_directory
from unbake.decomp.trial_target import inputs, owning_versions
from unbake.project.config import Held, Policy, Project, Unfinished, load_policy
from unbake.project_tools import atomic as atomic_files


def register(phases: Subparsers) -> None:
    parser = phases.add_parser("draft", phase="draft", help="Draft a function using the configured naming version.")
    parser.add_argument("function", nargs="?", metavar="FUNCTION")
    parser.add_argument("--scratch", type=Path, help="Private draft output directory outside the project.")
    parser.add_argument("--redraft", action="store_true", help="Regenerate an existing draft, archiving its source.")
    parser.add_argument("--exclude", type=Path, metavar="FILE", help="Override the project exclusion manifest.")
    parser.add_argument("--without-type-db", action="store_true", help="Diagnostic baseline: omit solved type context.")
    parser.add_argument("--struct", metavar="ID", help="Draft an evidenced shared struct (implementation pending).")


def run(args: argparse.Namespace, project: Project, policy: Policy) -> bool:
    if args.struct is not None:
        if args.function is not None:
            raise Held("draft", "draft.subject: select FUNCTION or --struct ID", next_action="unbake draft --help")
        raise Unfinished("draft", "draft.struct")
    function = args.function
    if function is None:
        raise Held("draft", "draft.function: supply FUNCTION", next_action="unbake next")
    if not re.fullmatch(r"[A-Za-z_]\w*", function):
        raise Held("draft", "draft.function: expected a C identifier", next_action="unbake next")
    if function in exclusions.load(project, getattr(args, "exclude", None)):
        raise Held("draft", f"draft.excluded: {function}: excluded by explicit manifest", next_action="unbake next")
    local = project.tools / "clone-policy.toml"
    if local.is_file():
        policy = load_policy(local)
    versions = owning_versions(project, function, None)
    naming = versions[0]
    remembered = draft_presence.attempts(project).get(function)
    previous = Path(remembered["scratch"]) if remembered and getattr(args, "redraft", False) else None
    scratch = scratch_directory(project, args.scratch or previous or default_scratch(project, policy), "draft")
    _database, context = ("", "") if args.without_type_db else type_context.snapshot(project, function)
    destination = scratch / "drafts" / function
    source = destination / (function + ".c")
    refresh = source.exists() and (getattr(args, "redraft", False) or function in type_context.redrafts(project))
    if source.exists() and not refresh:
        try:
            work.overlay_data(project, source)
        except Held as error:
            if not error.reason.startswith("trial.overlay_stale:"):
                raise
            refresh = True
    if source.exists() and not refresh:
        raise Held(
            "draft",
            f"draft.source: {source} already exists; edit and try it",
            next_action="unbake try " + shlex.quote(str(source)) + " --scratch " + shlex.quote(str(scratch)),
        )
    with inputs(project, function, versions, read_only=True) as pinned:
        draft_presence.remember(project, function, scratch, "failed draft attempt")
        generated = m2c.draft(
            project,
            policy,
            function,
            naming,
            scratch,
            generation=pinned[naming][0],
            type_context=context,
            announce=False,
            use_type_db=not args.without_type_db,
        )
        if refresh:
            archive = scratch / (function + ".redraft." + uuid4().hex)
            shutil.move(str(destination), archive)
        destination.mkdir(parents=True, exist_ok=True)
        atomic_files.copyfile(generated, source)
        atomic_files.copytree(generated.parent / "overlay", destination / "overlay")
        atomic_files.copyfile(generated.parent / "overlay.json", destination / "overlay.json")
        atomic_files.write(
            destination / "manifest.json",
            work.encoded(work.identity(project, source, versions, pinned=pinned, policy=policy)),
        )
    draft_presence.remember(project, function, scratch, "private draft")
    suggest(command(project.root, "try") + " " + shlex.quote(str(source)) + " --scratch " + shlex.quote(str(scratch)))
    return receipt("draft", [f"draft_path: {source}", f"versions: {', '.join(versions)}; draft version: {naming}"])
