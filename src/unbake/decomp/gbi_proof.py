"""Prove GBI source rewrites through the ordinary cached compiler path."""

from pathlib import Path

from unbake import atomic as atomic_files
from unbake import runner, scratch
from unbake.config import Held, Host, Project
from unbake.layout import split
from unbake.objects.elf import Object
from unbake.process import named as cause_named


def code(project: Project, policy: Host, version: str, source: Path, mode: int) -> object:
    """Compare allocated sections and relocation identities from cached objects."""
    with runner.compile_unit(project, policy, source, version, unit=source.stem, non_matching=bool(mode)) as output:
        obj = Object(output)
        return tuple(
            (
                name,
                obj.content(index),
                tuple(
                    (offset, kind, symbol["name"], symbol["value"]) for offset, kind, symbol in obj.relocations(index)
                ),
            )
            for index, name in enumerate(obj.names)
            if obj.sections[index][2] & 2
        )


def preserve(
    project: Project, policy: Host, unit: Path, before: str, after: str, *, versions: tuple[str, ...] | None = None
) -> None:
    holding = [
        version
        for version in project.versions
        if any(unit.stem in row.aliases for row in split.functions(project, version))
    ]
    versions = tuple(holding) if versions is None else versions
    if not versions or set(versions) - set(holding):
        raise Held(
            cause_named(
                f"{unit.stem}",
                f"{unit.stem}: no owning VERSION for codegen proof",
                owner="decomp.gbi_proof",
                stage="gbi",
            )
        )
    with scratch.temporary(policy, project, "gbi", prefix="gbi-proof-") as temporary:
        root = Path(temporary)
        original, candidate = root / "before" / unit.name, root / "after" / unit.name
        for path, text in ((original, before), (candidate, after)):
            path.parent.mkdir()
            atomic_files.text(path, text)
        for version in versions:
            for mode in (0, 1):
                if code(project, policy, version, original, mode) != code(project, policy, version, candidate, mode):
                    raise Held(
                        cause_named(
                            f"{unit.stem}",
                            f"{unit.stem}: VERSION {version} NON_MATCHING={mode}: rewrite changes codegen",
                            owner="decomp.gbi_proof",
                            stage="gbi",
                        )
                    )
