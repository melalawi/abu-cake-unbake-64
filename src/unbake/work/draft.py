"""draft: write build/work/FUNC/FUNC.c from the function's assembly with m2c and the solved types.

A published unit that breaks a source rule is drafted from its own src/ text instead (published_seed), with
m2c's prelude typedefs resolved to the shared types they name.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

from unbake import atomic as atomic_files
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
    """A published unit's own text when it still breaks a source rule; None when FUNC is unmatched.

    That text is the draft of a published unit: landing it again republishes the unit once it is exact and clean.
    """
    from unbake.decomp import checks, field_access, prelude
    from unbake.work import compare

    if not compare.published(project, function):
        return None
    source = project.src / f"{function}.c"
    if not checks.unmarked(source):
        raise Held("draft", f"draft.published: {function}: {source} is published and breaks no source rule")
    text = prelude.fields(prelude.resolve(source.read_text()))
    if "M2C_FIELD(" not in text:
        return text
    # Published helpers measure scalar storage, not aggregate identity. Avoid
    # reparsing the entire solved context for these explicitly typed accesses.
    local = draft_view(project, function)
    text, shared = field_access.share(local, function, text, "")
    if shared is not None:
        relative = shared.relative_to(local.include[0]).as_posix()
        text = f'#include "{relative}"\n' + text
    return text


def draft(project: Project, host: Host, function: str, *, replace: bool) -> Drafted:
    seed = published_seed(project, function)
    if seed is None and function in exclusions.load(project):
        raise Held("draft", f"draft.excluded: {function}: listed in {exclusions.MANIFEST}")
    versions = split.holding_versions(project, function)
    directory = attempts.directory(project, function)
    file = directory / f"{function}.c"
    if file.exists() and not replace:
        raise Held(
            "draft",
            f"draft.exists: {file} already exists; edit it, or redraft with --replace",
            next_action=f"unbake compare {file}",
        )
    if seed is not None:
        if replace:
            shutil.rmtree(directory / "include", ignore_errors=True)
        (directory / "include").mkdir(parents=True, exist_ok=True)
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
        atomic_files.text(file, unproven.read_text(encoding="utf-8"), encoding="utf-8")
        shutil.rmtree(scratch, ignore_errors=True)
        raise Held(
            "draft",
            f"draft.unproven: {file} does not compile yet; edit it: {error.reason}",
            next_action=f"unbake compare {file}",
        ) from error
    atomic_files.text(file, content, encoding="utf-8")
    shutil.rmtree(scratch, ignore_errors=True)
    return Drafted(function, file, versions)
