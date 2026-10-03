"""JSON snapshots of parsed headers; restore aggregate edges and layout cache identities."""

from __future__ import annotations

import inspect
import json
from collections.abc import Callable
from dataclasses import fields, is_dataclass
from pathlib import Path
from typing import Any

from unbake.layout.structs import Field, Layout
from unbake.layout.structs_parser import Parser
from unbake.layout.structs_types import Aggregate, Declaration, Member
from unbake.project.cache import Cache, key

_RECORDS = {cls.__name__: cls for cls in (Field, Layout, Aggregate, Declaration, Member)}
_FIELDS = {name: tuple(field.name for field in fields(cls)) for name, cls in _RECORDS.items()}


def _encode(value: Any) -> dict[str, Any]:
    records: list[Any] = []
    seen: dict[int, int] = {}
    strings: list[str] = []
    string_indices: dict[str, int] = {}

    def visit(item: Any) -> Any:
        # Every Layout.source points at the same complete header text. Repeating
        # it in JSON expands a few MiB into tens of GiB on large projects.
        if isinstance(item, str) and len(item) >= 128:
            if item not in string_indices:
                string_indices[item] = len(strings)
                strings.append(item)
            return {"string": string_indices[item]}
        if is_dataclass(item) and not isinstance(item, type):
            index = seen.get(id(item))
            if index is None:
                index = len(records)
                seen[id(item)] = index
                kind = type(item).__name__
                records.append(None)
                records[index] = [kind, [visit(getattr(item, name)) for name in _FIELDS[kind]]]
            return {"ref": index}
        if isinstance(item, tuple):
            return {"tuple": [visit(child) for child in item]}
        if isinstance(item, list):
            return [visit(child) for child in item]
        if isinstance(item, dict):
            return {"dict": [[name, visit(child)] for name, child in item.items()]}
        return item

    root = visit(value)
    return {"root": root, "records": records, "strings": strings}


def _decode(document: dict[str, Any]) -> Any:
    records = document["records"]
    objects = [object.__new__(_RECORDS[kind]) for kind, _ in records]

    def visit(item: Any) -> Any:
        if isinstance(item, list):
            return [visit(child) for child in item]
        if isinstance(item, dict):
            if "string" in item:
                return document["strings"][item["string"]]
            if "ref" in item:
                return objects[item["ref"]]
            if "tuple" in item:
                return tuple(visit(child) for child in item["tuple"])
            return {name: visit(child) for name, child in item["dict"]}
        return item

    for (kind, values), instance in zip(records, objects, strict=True):
        for name, item in zip(_FIELDS[kind], values, strict=True):
            object.__setattr__(instance, name, visit(item))
    return visit(document["root"])


def context(
    contents: dict[Path, str],
    root: Path | None,
    cache_root: Path,
    parse: Callable[[], tuple[dict[Path, str], Parser, list[Layout]]],
) -> tuple[dict[Path, str], Parser, list[Layout]]:
    """Cache only successfully parsed, complete inputs; temporary roots are not semantic inputs."""
    from unbake.decomp import draft_context, header_declarations
    from unbake.layout import header_context, structs, structs_parser, structs_types

    names = {
        path: path.relative_to(root).as_posix() if root and path.is_relative_to(root) else str(path)
        for path in contents
    }
    selection = json.dumps([(names[path], text) for path, text in contents.items()], separators=(",", ":"))
    drivers = (header_context, structs, structs_parser, structs_types, draft_context, header_declarations)
    identity = key("headers-context-v2", selection, Path(__file__), *(Path(inspect.getfile(module)) for module in drivers))
    computed = None

    def produce(output: Path) -> None:
        nonlocal computed
        computed = parse()
        ordered, parser, layouts = computed
        # Parser.cache uses live aggregate ids. Persist edges instead, then
        # rebuild its keys with the restored graph's ids in the next process.
        aggregates = {id(item): item for item in parser.aggregates}
        snapshot = {
            "source": parser.source,
            "types": parser.types,
            "aggregates": parser.aggregates,
            "declarations": parser.declarations,
            "defines": parser.defines,
            "cache": [(aggregates[index], layout) for index, layout in parser.cache.items() if index in aggregates],
            "layouts": layouts,
        }
        with output.open("w") as stream:
            json.dump({"order": [names[path] for path in ordered], "parser": _encode(snapshot)}, stream)

    artifact = Cache(cache_root).produce("header-context", identity, produce)
    if computed is not None:
        return computed
    with artifact.open() as stream:
        document = json.load(stream)
    paths = {name: path for path, name in names.items()}
    ordered = {paths[name]: contents[paths[name]] for name in document["order"]}
    snapshot = _decode(document["parser"])
    parser = Parser("")
    parser.source = snapshot["source"]
    parser.types = snapshot["types"]
    parser.aggregates = snapshot["aggregates"]
    parser.declarations = snapshot["declarations"]
    parser.defines = snapshot["defines"]
    parser.cache = {id(aggregate): layout for aggregate, layout in snapshot["cache"]}
    # The persisted parser is a completed declaration context, never a token
    # cursor. Headers seeds a fresh parser for each source it analyzes.
    return ordered, parser, snapshot["layouts"]
