"""tidy: rewrite a draft to use the shared types and macros; header changes stay private to the draft."""

from __future__ import annotations

from pathlib import Path

from unbake.config import Held, Host, Project
from unbake.fold import apply
from unbake import atomic as atomic_files
from unbake.work import compare


def tidy(project: Project, host: Host, file: Path) -> list[str]:
    function = compare.function_of(file)
    if not file.resolve().is_relative_to(project.work.resolve()):
        raise Held("tidy", f"tidy.file: {file}: expected a draft under {project.work}")
    folded = apply.fold(project, host, function, file.read_text())
    root = project.work / function / "include"
    for name, text in folded.headers.items():
        atomic_files.text(root / name, text)
    atomic_files.text(file, folded.source)
    return [*folded.notes, f"{file}: rewritten", *(f"private header: {root / name}" for name in sorted(folded.headers))]
