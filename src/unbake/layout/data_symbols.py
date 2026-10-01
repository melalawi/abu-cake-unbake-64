"""Resolve data correspondence between explicit VERSION symbol placements."""

from unbake.layout import split
from unbake.project.config import Held, Project


def counterparts(project: Project, name: str) -> dict[str, str]:
    """Use existing names or two agreeing surrounding cross-VERSION anchors."""
    source_version = project.names_from
    _, source = split.symbols(project.version(source_version).symbols)
    if name not in source:
        return {}
    address = source[name][0]
    result = {}
    for version in project.versions:
        _, target = split.symbols(project.version(version).symbols)
        if name in target:
            result[version] = name
            continue
        anchors = sorted(
            (value[0], target[key][0] - value[0])
            for key, value in source.items()
            if key in target and not key.startswith(("func_", "_")) and key != name
        )
        lower = [item for item in anchors if item[0] < address]
        upper = [item for item in anchors if item[0] > address]
        if not lower or not upper or lower[-1][1] != upper[0][1]:
            raise Held("split", f"data symbol {name}: no unambiguous correspondence in VERSION {version}")
        mapped = address + lower[-1][1]
        candidates = [key for key, value in target.items() if value[0] == mapped]
        if len(candidates) != 1:
            raise Held(
                "split", f"data symbol {name}: correspondence at 0x{mapped:X} is missing or ambiguous in {version}"
            )
        result[version] = candidates[0]
    return result
