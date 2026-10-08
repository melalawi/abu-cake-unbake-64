"""Resolve data correspondence between explicit VERSION symbol placements."""

import re
from typing import Any

from unbake.config import Held, Host, Project
from unbake.layout import split
from unbake.process import named as cause_named


def encoded_address(name: str) -> int | None:
    """An unnamed data label encodes its address in its naming VERSION."""
    return int(name[2:], 16) if re.fullmatch(r"D_[0-9A-Fa-f]{8}", name) else None


def _source_address(project: Project, name: str) -> int:
    _, source = split.symbols(project.version(project.names_from).symbols)
    if name in source:
        return source[name][0]
    address = encoded_address(name)
    if address is None:
        raise Held(
            cause_named(
                "layout.data_symbols._source_address",
                f"data symbol {name}: missing in names_from VERSION {project.names_from}",
                owner="layout.data_symbols",
                stage="split",
            )
        )
    return address


def mapped_address(
    source: dict[str, tuple[int, int, Any]], target: dict[str, tuple[int, int, Any]], name: str, address: int
) -> int:
    """The address in TARGET of the object at ADDRESS in SOURCE: the two nearest shared-name anchors
    must agree on one delta."""
    anchors = sorted(
        (value[0], target[key][0] - value[0])
        for key, value in source.items()
        if key in target and not key.startswith(("func_", "_")) and key != name
    )
    lower = [item for item in anchors if item[0] < address]
    upper = [item for item in anchors if item[0] > address]
    deltas = (
        {delta for base, delta in lower if base == lower[-1][0]}
        | {delta for base, delta in upper if base == upper[0][0]}
        if lower and upper
        else set()
    )
    if len(deltas) != 1:
        raise Held(
            cause_named(
                "layout.data_symbols.mapped_address",
                f"data symbol {name}: no unambiguous anchor delta",
                owner="layout.data_symbols",
                stage="split",
            )
        )
    return address + deltas.pop()


def in_version(project: Project, name: str, version: str) -> int | None:
    """The address in VERSION of a name that symbol file does not list but another VERSION does (a name
    another VERSION's address gave the object): the same object found through the shared-name anchors.
    None when no VERSION lists the name or the anchors disagree."""
    _, target = split.symbols(project.version(version).symbols)
    if name in target:
        return target[name][0]
    for origin in dict.fromkeys((project.names_from, *project.versions)):
        _, source = split.symbols(project.version(origin).symbols)
        if name in source and origin != version:
            try:
                return mapped_address(source, target, name, source[name][0])
            except Held:
                return None
    return None


def counterparts(project: Project, name: str) -> dict[str, str]:
    """Use existing names or two agreeing surrounding cross-VERSION anchors."""
    source_version = project.names_from
    _, source = split.symbols(project.version(source_version).symbols)
    address = _source_address(project, name)
    result = {}
    for version in project.versions:
        _, target = split.symbols(project.version(version).symbols)
        if name in target:
            result[version] = name
            continue
        try:
            mapped = mapped_address(source, target, name, address)
        except Held as error:
            raise Held(
                cause_named(
                    "layout.data_symbols.counterparts",
                    f"data symbol {name}: no unambiguous correspondence in VERSION {version}",
                    owner="layout.data_symbols",
                    stage="split",
                )
            ) from error
        candidates = [key for key, value in target.items() if value[0] == mapped]
        if len(candidates) != 1:
            raise Held(
                cause_named(
                    "layout.data_symbols.counterparts",
                    f"data symbol {name}: correspondence at 0x{mapped:X} is missing or ambiguous in {version}",
                    owner="layout.data_symbols",
                    stage="split",
                )
            )
        result[version] = candidates[0]
    return result


def addresses(project: Project, name: str) -> dict[str, int]:
    """Carry a data address through aligned references in corresponding code."""
    from unbake.decomp.symbols import references
    from unbake.layout import xver
    from unbake.project.rom import normalise
    from unbake.work.score import align_words, words

    name = split.name(name)
    source_version = project.names_from
    _, source = split.symbols(project.version(source_version).symbols)
    address = _source_address(project, name)
    result = {
        version: table[name][0]
        for version in project.versions
        if name in (table := split.symbols(project.version(version).symbols)[1])
    }
    if source_version in project.versions:
        result[source_version] = address
    missing = [version for version in project.versions if version not in result]
    if not missing:
        return result
    anchor_errors: dict[str, Held] = {}
    try:
        counterpart_names = counterparts(project, name)
    except Held as error:
        anchor_errors = dict.fromkeys(missing, error)
    else:
        for version in missing:
            result[version] = split.symbols(project.version(version).symbols)[1][counterpart_names[version]][0]
    missing = [version for version in missing if version not in result]
    if not missing:
        return result
    try:
        images = {
            version: normalise(project.version(version).baserom.read_bytes())
            for version in dict.fromkeys((source_version, *missing))
        }
    except (OSError, Held):
        raise anchor_errors[missing[0]] from None
    evidence: dict[str, set[int]] = {version: set() for version in missing}
    for function in split.functions(project, source_version):
        source_words = words(images[source_version][function.start : function.end])
        source_refs = references(source_words, source.get("_gp", (None,))[0])
        offsets = {ref.offset // 4 for ref in source_refs if ref.address == address}
        if not offsets:
            continue
        spans = xver.locate(project, function.name)
        masks = {
            index: 0xFFFF if word >> 26 != 3 else 0x03FFFFFF
            for index, word in enumerate(source_words)
            if word >> 26 in (3, 15) or index in {ref.offset // 4 for ref in source_refs}
        }
        for version in missing:
            span = spans.get(version)
            if span is None:
                continue
            target_words = words(images[version][span.start : span.end])
            _, target_symbols = split.symbols(project.version(version).symbols)
            target_refs = {
                ref.offset // 4: ref.address for ref in references(target_words, target_symbols.get("_gp", (None,))[0])
            }
            for tag, a, b, c, _ in align_words(target_words, source_words, masks):
                if tag == "equal":
                    evidence[version].update(
                        target_refs[a + index - c]
                        for index in offsets
                        if c <= index < c + b - a and a + index - c in target_refs
                    )
    for version, candidates in evidence.items():
        if len(candidates) != 1:
            raise Held(
                cause_named(
                    "layout.data_symbols.addresses",
                    f"data symbol {name}: no unambiguous aligned reference in VERSION {version}",
                    owner="layout.data_symbols",
                    stage="split",
                )
            )
        result[version] = candidates.pop()
    return result


def correspondence(project: Project, policy: Host, name: str) -> list[split.Edit]:
    """Compose each proved placement with the data-symbol command's edit owner."""
    from unbake.decomp.symbols_edits import data_symbol

    return [
        edit
        for version, address in addresses(project, name).items()
        for edit in data_symbol(project, policy, version, name, address, None)
    ]
