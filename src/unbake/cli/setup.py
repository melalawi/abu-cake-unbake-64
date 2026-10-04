"""setup: read the ROMs, plan the layout, propose compilers, and make the project ready."""

from __future__ import annotations

import argparse
import tomllib
from pathlib import Path

from unbake.cli.args import Context
from unbake.cli.output import Result
from unbake.config import Held

NAME = "setup"
HELP = "Read the ROMs and make the project ready (compilers, layout, first build)."
DESCRIPTION = """\
Read every ROM in roms/, name the versions, plan the function layout and propose a compiler per unit.
The first run stops with a proposal; review it, then confirm it with the digest it prints:

  unbake setup
  unbake setup --confirm <SHA256>

Other forms (each alone):
  unbake setup --list-compilers            show installed compilers and their pins
  unbake setup --redo-compilers            re-propose compilers on a ready project
  unbake setup --redo-symbol-matching      re-plan symbol correspondence across versions
"""
PROJECT = "pending"


def READ_ONLY(args: argparse.Namespace) -> bool:
    return bool(args.list_compilers)


def register(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--confirm", metavar="SHA256", help="Confirm the reviewed proposal digest.")
    parser.add_argument("--name-functions-from", metavar="VERSION", help="Version whose symbols name functions.")
    parser.add_argument("--rename-version", action="append", default=[], metavar="OLD=NEW")
    parser.add_argument("--version-order", metavar="V1,V2,...", help="Explicit version order.")
    parser.add_argument("--build-name", metavar="STEM", help="File stem of the built ROM.")
    parser.add_argument("--title", metavar="TITLE")
    parser.add_argument("--redo-compilers", action="store_true")
    parser.add_argument("--redo-symbol-matching", action="store_true")
    parser.add_argument("--keep-symbol-names", action="store_true", help="With --redo-symbol-matching only.")
    parser.add_argument("--list-compilers", action="store_true")
    parser.add_argument("--compiler-files", type=Path, metavar="PATH", help="Local compiler archive or directory.")
    parser.add_argument("--use-compiler", action="append", default=[], metavar="REGION=ID")


def pairs(values: list[str], flag: str) -> dict[str, str]:
    result = {}
    for value in values:
        if "=" not in value:
            raise Held("setup", f"{flag}: {value}: expected OLD=NEW")
        key, item = value.split("=", 1)
        if not key or not item or key in result:
            raise Held("setup", f"{flag}: {value}: empty or duplicate assignment")
        result[key] = item
    return result


def _facts_given(args: argparse.Namespace) -> bool:
    return bool(
        args.use_compiler
        or args.build_name
        or args.title
        or args.name_functions_from
        or args.rename_version
        or args.version_order
    )


def run(context: Context) -> Result:
    from unbake import config
    from unbake.compilers import refresh, registry
    from unbake.project import census, flow, setup, setup_config

    args = context.args
    host = context.require_host()
    project = context.pending()
    if args.keep_symbol_names and not args.redo_symbol_matching:
        raise Held("setup", "setup.symbol_layout: --keep-symbol-names requires --redo-symbol-matching")
    if args.list_compilers:
        rows = registry.status(host)
        return Result.ok(NAME, {"compilers": dict(rows)}, [f"{ident}: {state}" for ident, state in rows], None)
    if args.redo_symbol_matching:
        if project.state != "ready":
            raise Held("setup", "setup.symbol_layout: ready project required")
        if args.redo_compilers or _facts_given(args):
            raise Held("setup", "setup.symbol_layout: redo symbol matching separately from other setup changes")
        from unbake.layout import symbol_replan

        lines = symbol_replan.run(config.load(project.root), host, args.confirm, retain_names=args.keep_symbol_names)
        return Result.ok(NAME, {}, lines, context.cmd("next"))
    if args.redo_compilers:
        if project.state != "ready":
            raise Held("setup", "setup.compiler_refresh: ready project required")
        if _facts_given(args):
            raise Held("setup", "setup.compiler_refresh: cannot change game facts or force a compiler")
        lines = refresh.run(project, host, args.confirm)
        return Result.ok(NAME, {}, lines, context.cmd("next"))
    if project.state == "ready":
        if _facts_given(args):
            raise Held("setup", "setup.rom_set_changed: a ready project keeps its confirmed facts")
        lines = setup.refresh(project, host, supply=args.compiler_files)
        return Result.ok(NAME, {}, lines, context.cmd("next"))
    census.candidates(project)
    with (project.root / "config.toml").open("rb") as source:
        saved = tomllib.load(source)
    selected = args.name_functions_from or saved["project"].get("names_from")
    if selected is None:
        previous = census.ingest_manifest(project)
        if previous is not None:
            selected = previous.get("names_from")
    result = census.run(
        project,
        host,
        names_from=selected,
        renames=pairs(args.rename_version, "--rename-version") if args.rename_version else None,
        order=tuple(args.version_order.split(",")) if args.version_order is not None else None,
    )
    setup_config.write_facts(project, result, name=args.build_name, title=args.title)
    lines = [f"ROM folder: {project.roms}", f"census: {result.manifest}"]
    layout = flow.plan_layout(project, result, host)
    lines.extend(setup.layout_receipts(layout))
    proposal = flow.propose_compilers(
        project, result, layout, host, choices=pairs(args.use_compiler, "--use-compiler") or None
    )
    with setup.prepare_setup(
        project, result, layout, proposal, host, confirm=args.confirm, supply=args.compiler_files
    ) as prove:
        lines.extend(prove())
    return Result.ok(NAME, {"ready": True}, lines, context.cmd("next"))
