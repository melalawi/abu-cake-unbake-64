"""Validate and order installed header declarations before measuring layouts."""

from __future__ import annotations

import re
from bisect import bisect_right
from dataclasses import replace
from pathlib import Path
from typing import Any

from unbake.decomp.draft_context import ordered_headers
from unbake.layout.structs import Field, Layout
from unbake.layout.structs_identity import Index
from unbake.layout.structs_parser import Parser
from unbake.layout.structs_types import Aggregate
from unbake.project.config import Held
from unbake.project.headers import include_headers


def context(contents: dict[Path, str], *, root: Path | None = None) -> tuple[dict[Path, str], Parser, list[Layout]]:
    """Reuse draft's declaration parser; retain raw spans for header edits.

    One parse per distinct header set in a process; the parser is shared and read-only.
    """
    from unbake.project.cache import remembered

    ordered, parser, records = remembered(
        "headers.context", (root, tuple(sorted(contents.items()))), lambda: _context(contents, root=root)
    )
    return dict(ordered), parser, records


def _context(contents: dict[Path, str], *, root: Path | None) -> tuple[dict[Path, str], Parser, list[Layout]]:
    try:
        contents = {path: contents[path] for path in ordered_headers(contents)}
        parser = Parser("\n".join(contents.values()))
        records = parser.parse()
    except Held as error:
        reason = error.reason
        if root is not None:
            for path in contents:
                if path.is_relative_to(root):
                    reason = reason.replace(str(path), path.relative_to(root).as_posix())
        line = re.search(r"\bline (\d+)", reason)
        if line is not None:
            number = int(line[1])
            for path, text in contents.items():
                lines = text.count("\n") + 1
                if number <= lines:
                    label = path.relative_to(root) if root is not None and path.is_relative_to(root) else path
                    reason = reason[: line.start()] + f"{label}:{number}" + reason[line.end() :]
                    break
                number -= lines
        raise Held("structs", f"headers.declaration: SDK/shared header prerequisite: {reason}") from error
    return contents, parser, records


def _shift_field(field: Field, delta: int) -> Field:
    return replace(
        field,
        start=field.start + delta,
        end=field.end + delta,
        fields=tuple(_shift_field(child, delta) for child in field.fields),
    )


def _homes(texts: dict[Path, str], parser: Parser, records: list[Layout]) -> dict[str, Path]:
    """Map each aggregate tag, alias and typedef name to the header that declares it."""
    homes: dict[str, Path] = {}
    starts: list[int] = []
    ends: list[int] = []
    paths: list[Path] = []
    cursor = 0
    for path, text in texts.items():
        starts.append(cursor)
        ends.append(cursor + len(text))
        paths.append(path)
        cursor += len(text) + 1

    def owner(offset: int) -> Path | None:
        index = bisect_right(starts, offset) - 1
        return paths[index] if index >= 0 and offset < ends[index] else None

    for record in records:
        home = owner(record.start)
        if home is not None:
            for name in (record.name, *record.aliases):
                homes.setdefault(name, home)
    for declaration in parser.declarations:
        value = parser.source[declaration.start : declaration.end]
        if not value.lstrip().startswith("typedef "):
            continue
        found = re.search(r"\(\s*\*\s*(\w+)\s*\)", value) or re.search(r"\b(\w+)\s*(?:\[[^]]*\]\s*)*;\s*$", value)
        home = owner(declaration.start)
        if found and home is not None:
            homes.setdefault(found[1], home)
    return homes


def _placed(texts: dict[Path, str], records: list[Layout]) -> list[tuple[Path, Layout, int]]:
    """Each record with its header and that header's offset in the combined source."""
    starts: list[int] = []
    paths: list[Path] = []
    cursor = 0
    for path, text in texts.items():
        starts.append(cursor)
        paths.append(path)
        cursor += len(text) + 1
    result = []
    for record in records:
        index = bisect_right(starts, record.start) - 1
        if index >= 0 and record.start < starts[index] + len(texts[paths[index]]):
            result.append((paths[index], record, starts[index]))
    return result


def _scalar_header(text: str) -> tuple[bool, dict[str, str]] | None:
    """Scalar typedef homes: aggregate-free headers and whether they are guarded."""
    from unbake.layout.structs_types import SCALARS

    if re.search(r"\b(?:struct|union)\b", re.sub(r"/\*.*?\*/|//[^\n]*", "", text, flags=re.S)):
        return None
    parser = Parser(text)
    parser.parse()
    scalars = {
        name: parser.type_name(target[0], target[1])
        for name, target in parser.types.items()
        if isinstance(target, tuple) and parser.type_name(target[0], target[1]) in SCALARS
    }
    if not scalars:
        return None
    return bool(re.search(r"^\s*#\s*ifndef\b", text, re.M)), scalars


class Headers:
    """One parsed include context for a batch of folds.

    The project's headers are parsed once. A fold that only adds a new header is
    parsed alone against the accumulated types and appended; a fold that edits
    an existing header reparses the whole context. Records keep offsets into the
    combined source, so callers locate declarations exactly as with context().
    """

    def __init__(self, texts: dict[Path, str], *, root: Path | None) -> None:
        self.root = root
        self._load(texts)

    def _load(self, texts: dict[Path, str]) -> None:
        ordered, parser, records = context(texts, root=self.root)
        self.texts = ordered
        self.source = parser.source
        self.types = dict(parser.types)
        self.cache = dict(parser.cache)
        self.defines = dict(parser.defines)
        self.declarations = list(parser.declarations)
        self.records = list(records)
        self.homes = _homes(ordered, parser, records)
        self.placed = _placed(ordered, records)
        self.locations: dict[str, list[tuple[Path, Layout, int]]] = {}
        for entry in self.placed:
            self._locate(entry)
        self.scalars = {path: _scalar_header(text) for path, text in ordered.items()}
        self.index = Index(self.records)
        self._aliased: dict[int, Aggregate] = {}
        self._aliases(self.types)
        self.sdk = {
            name
            for path, record, _ in self.placed
            if path.name == "n64sdk.h"
            for name in (record.name, *record.aliases)
        }
        self.tag_only = {record.name for record in self.records if record.name not in record.aliases}

    @classmethod
    def read(cls, project: Any) -> Headers:
        texts = {path: path.read_text() for path, _ in include_headers(project)}
        return cls(texts, root=getattr(project, "root", None))

    def seeded(self, text: str) -> Parser:
        """A parser for text that sees every type declared by these headers."""
        parser = Parser(text)
        local = set(re.findall(r"\b((?:struct|union)\s+\w+)\s*\{", text))
        parser.types.update({name: value for name, value in self.types.items() if name not in local})
        parser.cache.update(self.cache)
        parser.defines = {**self.defines, **parser.defines}
        return parser

    def parse(self, text: str) -> tuple[Parser, list[Layout]]:
        """Parse text against these headers without growing shared alias lists."""
        parser = self.seeded(text)
        try:
            return parser, parser.parse()
        finally:
            # Every parse revisits the seeded typedefs and re-adds their aliases to
            # the shared aggregates; keep each alias once.
            for aggregate in self._aliased.values():
                if len(aggregate.aliases) > 1:
                    aggregate.aliases[:] = list(dict.fromkeys(aggregate.aliases))

    def _aliases(self, types: dict[str, Any]) -> None:
        for value in types.values():
            target = value if isinstance(value, Aggregate) else value[0] if not value[1] else None
            if isinstance(target, Aggregate):
                self._aliased.setdefault(id(target), target)

    def apply(self, edits: list[Any]) -> None:
        """Adopt header edits, appending new files and reparsing for changed ones."""
        changed = {edit.path: edit.after for edit in edits if edit.path in self.texts}
        added = {edit.path: edit.after for edit in edits if edit.path not in self.texts}
        if any(self.texts[path] != text for path, text in changed.items()):
            self._load({**self.texts, **changed, **added})
            return
        for path, text in added.items():
            self._append(path, text)

    def _append(self, path: Path, text: str) -> None:
        try:
            parser, records = self.parse(text)
        except Held as error:
            label = path.relative_to(self.root) if self.root is not None and path.is_relative_to(self.root) else path
            raise Held("structs", f"headers.declaration: {label}: {error.reason}") from error
        delta = len(self.source) + 1
        shifted = [
            replace(
                record,
                start=record.start + delta,
                end=record.end + delta,
                body_start=record.body_start + delta,
                body_end=record.body_end + delta,
                fields=tuple(_shift_field(field, delta) for field in record.fields),
            )
            for record in records
        ]
        self.texts[path] = text
        self.source = self.source + "\n" + text
        self.types.update(parser.types)
        self._aliases(parser.types)
        self.cache.update(parser.cache)
        self.defines.update(parser.defines)
        self.declarations.extend(
            replace(declaration, start=declaration.start + delta, end=declaration.end + delta)
            for declaration in parser.declarations
        )
        self.records.extend(shifted)
        for record in shifted:
            self.placed.append((path, record, delta))
            self._locate(self.placed[-1])
        for name, home in _homes({path: text}, parser, records).items():
            self.homes.setdefault(name, home)
        self.scalars[path] = _scalar_header(text)
        self.index.add(shifted)
        if path.name == "n64sdk.h":
            self.sdk.update(name for record in shifted for name in (record.name, *record.aliases))
        self.tag_only.update(record.name for record in shifted if record.name not in record.aliases)

    def _locate(self, entry: tuple[Path, Layout, int]) -> None:
        for name in (entry[1].name, *entry[1].aliases):
            self.locations.setdefault(name, []).append(entry)

    def parser(self) -> Parser:
        """A read-only combined parser view for code written against context()."""
        parser = Parser("")
        parser.source = self.source
        parser.types = self.types
        parser.cache = self.cache
        parser.defines = self.defines
        parser.declarations = self.declarations
        return parser
