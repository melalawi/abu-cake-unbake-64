"""Prove GBI source rewrites through the ordinary cached compiler path."""

import tempfile
from pathlib import Path

from unbake.layout import split
from unbake.project import build
from unbake.project.config import Held, Policy, Project
from unbake.project_tools import atomic as atomic_files
from unbake.project_tools.elf import Object


def code(project: Project, policy: Policy, version: str, source: Path, mode: int) -> object:
    """Compare allocated sections and relocation identities from cached objects."""
    output = source.with_suffix(".o")
    build.compile_object(project, policy, source, version, output, non_matching=bool(mode))
    obj = Object(output)
    return tuple(
        (
            name,
            obj.content(index),
            tuple((offset, kind, symbol["name"], symbol["value"]) for offset, kind, symbol in obj.relocations(index)),
        )
        for index, name in enumerate(obj.names)
        if obj.sections[index][2] & 2
    )


def preserve(project: Project, policy: Policy, unit: Path, before: str, after: str) -> None:
    versions = [
        version
        for version in project.versions
        if any(unit.stem in row.aliases for row in split.functions(project, version))
    ]
    if not versions:
        raise Held("gbi", f"{unit.stem}: no owning VERSION for codegen proof")
    with tempfile.TemporaryDirectory(prefix="gbi-proof-") as temporary:
        root = Path(temporary)
        original, candidate = root / "before" / unit.name, root / "after" / unit.name
        for path, text in ((original, before), (candidate, after)):
            path.parent.mkdir()
            atomic_files.text(path, text)
        for version in versions:
            for mode in (0, 1):
                if code(project, policy, version, original, mode) != code(project, policy, version, candidate, mode):
                    raise Held("gbi", f"{unit.stem}: VERSION {version} NON_MATCHING={mode}: rewrite changes codegen")
