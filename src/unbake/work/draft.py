"""draft: write build/work/FUNC/FUNC.c from the function's assembly with m2c and the solved types.

A published unit that breaks a source rule is drafted from its own src/ text instead (published_seed), with
m2c's prelude typedefs resolved to the shared types they name.
"""

from __future__ import annotations

import re
import shutil
from dataclasses import dataclass
from pathlib import Path

from unbake import atomic as atomic_files
from unbake import cache as retention
from unbake import extract
from unbake.config import Held, Host, Project, draft_view
from unbake.decomp import exclusions, m2c, type_context
from unbake.layout import split
from unbake.work import attempts


@dataclass(frozen=True)
class Drafted:
    function: str
    file: Path
    versions: tuple[str, ...]


def naming_version(project: Project, versions: tuple[str, ...]) -> str:
    """Draft from the version that names functions when it holds this one, else the first holder."""
    return project.names_from if project.names_from in versions else versions[0]


def published_seed(project: Project, function: str) -> str | None:
    """A published unit's own text when some version is still assembly or it breaks a source rule.

    None when FUNC is unmatched. Reuse the proved text for partial publications; landing again must preserve
    every already published version as well as proving any new ones.
    """
    from unbake.decomp import checks, prelude
    from unbake.work import compare

    versions = split.holding_versions(project, function)
    kinds = [compare.row_of(project, function, version).kind for version in versions]
    if attempts.fuzzy(project, function) is not None:
        return attempts.unguarded((project.src / f"{function}.c").read_text())
    if "c" not in kinds:
        return None
    source = project.src / f"{function}.c"
    if all(kind == "c" for kind in kinds) and not checks.unmarked(source):
        raise Held("draft", f"draft.published: {function}: {source} is published and breaks no source rule")
    return prelude.fields(prelude.resolve(source.read_text()))


def check_existing(project: Project, function: str, *, replace: bool) -> str | None:
    """A cheap refusal; the backend repeats it after prerequisites to protect against intervening writes."""
    file = project.work / function / f"{function}.c"
    if file.exists() and not replace:
        raise Held(
            "draft",
            f"draft.exists: {file} already exists; edit it, or redraft with --replace",
            next_action=f"unbake compare {file}",
            data={
                "phase": "preflight",
                "blocked_before_build": True,
                "built": False,
                "work": {"source_scans": 0, "step_runs": 0, "make_invocations": 0},
            },
        )
    from unbake import inputs

    return inputs.digest(file, algorithm="sha256", reuse=retention.configured()) if file.is_file() else None


def draft(project: Project, host: Host, function: str, *, replace: bool, expected_output: str | None) -> Drafted:
    pin = check_existing(project, function, replace=replace)
    if pin != expected_output:
        raise Held("draft", "draft.changed: existing draft changed during preparation")

    def guard() -> None:
        if check_existing(project, function, replace=replace) != pin:
            raise Held("draft", "draft.changed: output changed since preparation")

    seed = published_seed(project, function)
    if seed is None and function in exclusions.load(project):
        raise Held("draft", f"draft.excluded: {function}: listed in {exclusions.MANIFEST}")
    versions = split.holding_versions(project, function)
    directory = attempts.directory(project, function)
    file = directory / f"{function}.c"
    if seed is not None:
        if replace:
            shutil.rmtree(directory / "include", ignore_errors=True)
        (directory / "include").mkdir(parents=True, exist_ok=True)
        if "M2C_FIELD(" in seed:
            from unbake.decomp import field_access

            # Generate the shared measured storage only after --replace has
            # cleared stale private headers. Aggregate identity is unknown.
            local = draft_view(project, function)
            seed, shared = field_access.share(local, function, seed, "")
            if shared is not None:
                relative = shared.relative_to(local.include[0]).as_posix()
                includes = list(re.finditer(r"^[ \t]*#[ \t]*include[^\n]*\n", seed, re.M))
                boundary = includes[-1].end() if includes else 0
                seed = seed[:boundary] + f'#include "{relative}"\n' + seed[boundary:]
        guard()
        atomic_files.text(file, seed, encoding="utf-8")
        return Drafted(function, file, versions)
    version = naming_version(project, versions)
    _digest, context = type_context.snapshot(project, function)
    extracted = extract.directory(project, host, version)
    scratch = directory / ".m2c"
    if scratch.exists():
        shutil.rmtree(scratch)
    if replace and (directory / "include").exists():
        shutil.rmtree(directory / "include")
    # The draft's own header directory comes first on its include path (config.draft_view).
    (directory / "include").mkdir(parents=True, exist_ok=True)
    try:
        content = m2c.draft(
            draft_view(project, function), host, function, version, scratch, extracted, type_context=context
        )
    except Held as error:
        # A complete draft that does not compile yet is still the thing to edit: put it where compare and
        # the cycle watcher look, and say so.
        unproven = scratch / "compile-proof" / f"{function}.c"
        if not unproven.is_file():
            raise
        guard()
        atomic_files.text(file, unproven.read_text(encoding="utf-8"), encoding="utf-8")
        shutil.rmtree(scratch, ignore_errors=True)
        raise Held(
            "draft",
            f"draft.unproven: {file} does not compile yet; edit it: {error.reason}",
            next_action=f"unbake compare {file}",
        ) from error
    guard()
    atomic_files.text(file, content, encoding="utf-8")
    shutil.rmtree(scratch, ignore_errors=True)
    return Drafted(function, file, versions)
