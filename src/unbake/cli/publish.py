"""publish: land exact functions or retain admitted fuzzy C through the same writer."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from unbake.cli.args import Context
from unbake.cli.output import Result
from unbake.process import named as cause_named

NAME = "publish"
HELP = "Publish exact FILEs or retain fuzzy C with --fuzzy."
DESCRIPTION = """\
Land each file whose function is exact in every version. For each one: build the ROM of every
holding version with the new C, compare it with the original, and only then write src/FUNC.c,
update layout.toml and commit "Match FUNC". A file that does not prove is not written.

  unbake publish build/work/func_80012345/func_80012345.c
  unbake publish build/work/a/a.c build/work/b/b.c
  unbake publish --original osInvalDCache

--original lands an original-asm function (one no configured compiler emits from C) as byte-exact
src/FUNC.s, records its rule in unbake-original-asm.json and commits "Original asm FUNC".

--require-version VERSION (repeatable) allows C publication when those versions are exact.
Other exact versions land too; nonmatching versions retain assembly. Every already published
C version must remain exact. Compare still measures every holding version. The default and
--original continue to require every holding version.

--events streams a fn.committed JSON receipt immediately after each successful commit,
before merging or trying another file, followed by the usual final Result. The receipt
names the proved versions and input hashes for commit readback. Prefer one FILE per
invocation when dispatching each immutable commit to a separate publisher.

--compare measures and records each file through public compare immediately before
publication. --push REMOTE pushes the resulting commits to [publish].branch. When
that branch moved concurrently, publish rebases and rechecks only affected native
function/version proofs before pushing. Use publish --push REMOTE without FILEs
when retrying already committed work. An interrupted result has no final cause;
its ready list remains eligible for retry.

--fuzzy retains source that passes source/ABI checks and compiles in every holding
version, without a minimum matching percentage. Its body is guarded by NON_MATCHING;
the default ROM build keeps the original assembly rows and exact progress does not
increase. A later fuzzy source must have a higher measured, size-weighted score, or
an equal score while removing committed source-rule violations and adding none.
An unavailable comparison stays explicit and never authorizes replacement.
"""
PROJECT = "ready"


def READ_ONLY(args: argparse.Namespace) -> bool:
    return False


def register(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("files", type=Path, nargs="*", metavar="FILE")
    parser.add_argument("--compare", action="store_true", help="Measure each FILE before publication.")
    parser.add_argument(
        "--push", metavar="REMOTE", help="Rebase, reconcile affected proofs and push to the configured branch."
    )
    parser.add_argument("--events", action="store_true", help="Flush each commit receipt immediately as JSONL.")
    parser.add_argument(
        "--fuzzy", action="store_true", help="Retain admitted nonmatching C without changing default ROM bytes."
    )
    parser.add_argument(
        "--require-version",
        action="append",
        metavar="VERSION",
        help="Require this version and preserve already published versions; repeatable.",
    )
    parser.add_argument(
        "--original", action="append", default=[], metavar="FUNC", help="Land an original-asm function as src/FUNC.s."
    )


def run(context: Context) -> Result:
    from unbake import land
    from unbake.config import Held
    from unbake.cycle.events import Emitter

    if not context.args.files and not context.args.original and not getattr(context.args, "push", None):
        raise Held(
            cause_named("publish", "publish: name a FILE or --original FUNC", owner="cli.publish", stage="publish")
        )
    emitter = Emitter(context.stdout) if context.args.events else None

    def committed(record: dict[str, Any]) -> None:
        if emitter is not None:
            emitter.emit("fn.committed", **record)

    done = land.publish(
        context.project(),
        context.require_host(),
        [path.resolve() for path in context.args.files],
        originals=tuple(context.args.original),
        required_versions=(tuple(context.args.require_version) if context.args.require_version is not None else None),
        on_commit=committed if emitter is not None else None,
        **({"fuzzy": True} if context.args.fuzzy else {}),
        **({"compare_first": True} if getattr(context.args, "compare", False) else {}),
    )
    following = context.cmd("next")
    data = done.document()
    if done.interrupted:
        # Preserve scope and push options in the retry command.
        words = ["publish", *(str(path) for path in context.args.files if path.stem in done.ready)]
        for name in context.args.original:
            if name in done.ready:
                words.extend(("--original", name))
        for version in context.args.require_version or ():
            words.extend(("--require-version", version))
        if context.args.fuzzy:
            words.append("--fuzzy")
        if getattr(context.args, "compare", False):
            words.append("--compare")
        if getattr(context.args, "push", None):
            words.extend(("--push", context.args.push))
        retry = (
            context.cmd(*words)
            if done.ready or getattr(context.args, "push", None)
            else context.cmd("recompute", "merge-units")
        )
        return Result.interrupted(NAME, retry, data)
    remote = getattr(context.args, "push", None)
    if remote and (done.commits or (not context.args.files and not context.args.original)):
        from unbake.project import publication_push

        try:
            data["push"] = publication_push.push(context.project(), context.require_host(), remote)
        except KeyboardInterrupt:
            return Result.interrupted(NAME, context.cmd("publish", "--push", remote), data)
        except Held as error:
            return Result.held(NAME, error, context.cmd("publish", "--push", remote), data)
    result = Result.ok(NAME, data, done.lines(), following)
    failure = next(iter(done.failed.values())) if done.failed else done.post_commit_failure
    if failure is not None:
        return Result(NAME, "held", failure["key"], data, following, tuple(done.lines()))
    return result
