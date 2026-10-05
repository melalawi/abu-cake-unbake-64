"""A unit's layout records, resumed from the layouts of a header prefix it shares with earlier units.

A prefix state holds the layout parser after the prefix's declarations, the prefix's own layouts and
their records. Units share the prefix's aggregate objects, so each unit first restores them from the
state's snapshot. When the rest of a unit only adds new names, the prefix's layouts and records are
reused and only their alias lists change. Otherwise every layout is computed again.
"""

from __future__ import annotations

import dataclasses
import re
from dataclasses import dataclass
from typing import Any

from unbake import prefixes
from unbake.cdecl import LayoutParser, declaration_source, declarations, layout_tokens
from unbake.config import Held
from unbake.layout.structs import Layout
from unbake.layout.structs_types import Aggregate

_WORD = re.compile(r"[A-Za-z_]\w*")
Snapshot = list[tuple[Aggregate, tuple[Any, ...], tuple[str, ...], int, int, int, int, bool, str]]


@dataclass
class _State:
    tokens: list[re.Match[str]]
    types: dict[str, Any]
    aggregates: list[Aggregate]
    declarations: list[Any]
    snapshot: Snapshot
    # The prefix's own results, or None when its layouts or records refused.
    layouts: dict[int, Layout] | None
    rows: dict[int, dict[str, Any]] | None
    # The alias lookups the rows made, by name (None for a missing name).
    reads: dict[str, str | None]


class _Recorded(dict[str, str]):
    """An alias map that remembers every name looked up in it."""

    def __init__(self, values: dict[str, str]) -> None:
        super().__init__(values)
        self.names: set[str] = set()

    def get(self, name: str, default: Any = None) -> Any:
        self.names.add(name)
        return super().get(name, default)

    def __contains__(self, name: object) -> bool:
        self.names.add(str(name))
        return super().__contains__(name)

    def __getitem__(self, name: str) -> str:
        self.names.add(name)
        return super().__getitem__(name)


def _snapshot(types: dict[str, Any], aggregates: list[Aggregate]) -> Snapshot:
    found: dict[int, Aggregate] = {id(item): item for item in aggregates}
    for value in types.values():
        base = value[0] if isinstance(value, tuple) else value
        if isinstance(base, Aggregate):
            found[id(base)] = base
    return [
        (
            item,
            tuple(item.members),
            tuple(item.aliases),
            item.start,
            item.end,
            item.body_start,
            item.body_end,
            item.complete,
            item.name,
        )
        for item in found.values()
    ]


def _apply(snapshot: Snapshot) -> None:
    for item, members, aliases, start, end, body_start, body_end, complete, name in snapshot:
        item.members[:] = members
        item.aliases[:] = aliases
        item.start, item.end, item.body_start, item.body_end = start, end, body_start, body_end
        item.complete, item.name = complete, name


def _unchanged(state: _State, parser: LayoutParser) -> bool:
    """The rest of the unit only added names: no prefix name rebound and no prefix aggregate grown."""
    if any(parser.types.get(name) is not value for name, value in state.types.items()):
        return False
    return all(
        len(item.members) == len(members) and item.complete == complete and item.name == name
        for item, members, _, _, _, _, _, complete, name in state.snapshot
    )


def _typedefs(declaration: str, aliases: dict[str, str]) -> dict[str, str]:
    from unbake.typemap.declarations import canonical

    return {
        name: canonical(aliases[name], aliases) for name in sorted(declarations(declaration).uses) if name in aliases
    }


def _row(layout: Layout, source: str, aliases: dict[str, str]) -> dict[str, Any]:
    declaration = source[layout.start : layout.end] + ";"
    return {
        "type": f"{layout.kind} {layout.name}",
        "size": layout.size,
        "alignment": layout.alignment,
        "aliases": list(layout.aliases),
        "declaration": declaration,
        "typedefs": _typedefs(declaration, aliases),
        "fields": [
            {"name": f.name, "type": f.type, "offset": f.offset, "size": f.size, "extent": list(f.extent)}
            for f in layout.fields
        ],
    }


def _parser(state: _State | None, source: str, clean: str, start: int, end: int) -> LayoutParser:
    tokens = layout_tokens(clean, start, end)
    if state is None:
        return LayoutParser(source, tokens=tokens)
    _apply(state.snapshot)
    parser = LayoutParser(source, tokens=state.tokens + tokens)
    parser.types = dict(state.types)
    parser.aggregates = list(state.aggregates)
    parser.declarations = list(state.declarations)
    parser.index = len(state.tokens)
    return parser


def _results(
    state: _State | None, parser: LayoutParser, source: str, aliases: dict[str, str]
) -> tuple[dict[int, Layout], list[tuple[int, Layout, dict[str, Any] | None]], Held | None]:
    """Every named layout in order with its record; a refusal stops the records where it occurs."""
    reuse = state is not None and state.layouts is not None and _unchanged(state, parser)
    if reuse:
        assert state is not None and state.layouts is not None
        parser.cache.update(state.layouts)
    named = [item for item in parser.aggregates if item.name]
    layouts = parser.layouts()
    rows_reusable = (
        reuse
        and state is not None
        and state.rows is not None
        and all(dict.get(aliases, name) == value for name, value in state.reads.items())
    )
    results: list[tuple[int, Layout, dict[str, Any] | None]] = []
    for item, layout in zip(named, layouts, strict=True):
        current = tuple(dict.fromkeys(item.aliases))
        known = state.layouts.get(id(item)) if reuse and state is not None and state.layouts is not None else None
        row = None
        if known is not None and layout is known:
            if rows_reusable and state is not None and state.rows is not None:
                row = state.rows.get(id(item))
            if layout.aliases != current:
                layout = dataclasses.replace(layout, aliases=current)
                if row is not None:
                    row = {**row, "aliases": list(current)}
        if row is None:
            try:
                row = _row(layout, source, aliases)
            except Held as error:
                return dict(parser.cache), results, error
        results.append((id(item), layout, row))
    return dict(parser.cache), results, None


def records(source: str, aliases: dict[str, str]) -> tuple[dict[str, dict[str, Any]], list[str]]:
    """Layout records (without provenance) of a cleaned unit, and the layout refusal if any."""

    clean = prefixes.concatenated("layouts.declaration_source", source, declaration_source)

    def advance(state: _State | None, text: str, start: int, end: int) -> _State | None:
        parser = _parser(state, text, clean, start, end)
        try:
            parser.declare(len(parser.tokens))
        except Held:
            return None
        if parser.index != len(parser.tokens):
            return None
        types, aggregates, declared = dict(parser.types), list(parser.aggregates), list(parser.declarations)
        snapshot = _snapshot(types, aggregates)
        recorded = _Recorded(aliases)
        try:
            cache, results, refusal = _results(state, parser, text, recorded)
        except Held:
            return _State(parser.tokens, types, aggregates, declared, snapshot, None, None, {})
        rows = None if refusal is not None else {key: row for key, _, row in results if row is not None}
        names = recorded.names | (set(state.reads) if state is not None else set())
        reads = {name: aliases.get(name) for name in names}
        return _State(parser.tokens, types, aggregates, declared, snapshot, cache, rows, reads)

    def finish(state: _State | None, text: str, start: int) -> tuple[dict[str, dict[str, Any]], list[str]]:
        parser = _parser(state, text, clean, start, len(text))
        rows: dict[str, dict[str, Any]] = {}
        try:
            parser.declare(len(parser.tokens))
            _, results, refusal = _results(state, parser, text, aliases)
        except Held as error:
            return rows, ["types.layout: " + error.reason]
        for _, layout, row in results:
            assert row is not None
            rows[layout.name] = row
        return rows, [] if refusal is None else ["types.layout: " + refusal.reason]

    return prefixes.resumed("layouts", source, advance, finish)
