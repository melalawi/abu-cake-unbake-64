"""draft: write build/work/FUNC/FUNC.c from the function's assembly with m2c and the solved types."""

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


def draft(project: Project, host: Host, function: str, *, replace: bool) -> Drafted:
    if function in exclusions.load(project):
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
    content = m2c.draft(
        draft_view(project, function), host, function, version, scratch, extracted, type_context=context
    )
    atomic_files.text(file, content, encoding="utf-8")
    shutil.rmtree(scratch, ignore_errors=True)
    return Drafted(function, file, versions)
