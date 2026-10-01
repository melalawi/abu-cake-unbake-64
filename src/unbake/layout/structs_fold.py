"""Fold proved fields into shared include declarations."""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any

from unbake.layout import shared
from unbake.layout.split import Edit
from unbake.layout.structs import Field, Layout, held
from unbake.layout.structs_parser import Parser
from unbake.layout.structs_types import SCALARS, Aggregate


def _leaves(fields: tuple[Field, ...], offset: int = 0, prefix: str = "") -> Iterator[tuple[str, Field, int]]:
    for item in fields:
        name = f"{prefix}.{item.name}".strip(".")
        if item.fields and not item.extent:
            yield from _leaves(item.fields, offset + item.offset, name)
        else:
            yield name, item, offset + item.offset


def _padding(item: Field) -> bool:
    return bool(re.match(r"^(?:pad|padding)\w*$", item.name)) and item.type.split("[", 1)[0] in (
        "char",
        "u8",
        "s8",
        "unsigned char",
    )


def _scalar_headers(texts: dict[Path, str]) -> list[tuple[bool, dict[str, str], Path]]:
    candidates = []
    for path, text in texts.items():
        parser = Parser(text)
        if any(token[0] in ("struct", "union") for token in parser.tokens):
            continue
        parser.parse()
        scalars = {
            name: parser.type_name(target[0], target[1])
            for name, target in parser.types.items()
            if isinstance(target, tuple) and parser.type_name(target[0], target[1]) in SCALARS
        }
        if scalars:
            guarded = bool(re.search(r"^\s*#\s*ifndef\b", text, re.M))
            candidates.append((guarded, scalars, path))
    return candidates


def _scalar_include(project: Any, texts: dict[Path, str], records: list[Layout]) -> str:
    """Prefer a guarded scalar home: older compilers reject repeated typedefs."""
    candidates = _scalar_headers(texts)
    required = set(
        re.findall(
            r"\b[A-Za-z_]\w*\b",
            " ".join(member.declaration for record in records for _, member, _ in _leaves(record.fields)),
        )
    )
    if not candidates:
        return ""
    required &= set().union(*(names.keys() for _, names, _ in candidates))
    compatible = [(not guarded, -len(names), path) for guarded, names, path in candidates if required <= names.keys()]
    if not compatible:
        held("scalar headers", "no common declaration home for " + ", ".join(sorted(required)))
    path = min(compatible)[2]
    relative = next(path.relative_to(root) for root in project.include if path.is_relative_to(root))
    return f'#include "{relative.as_posix()}"\n'


def scalar_edits(project: Any, parser: Parser) -> tuple[set[str], list[tuple[int, int, str]]]:
    """Plan matching scalar typedef removal and required project header includes."""
    headers = {path: path.read_text() for root in project.include for path in Path(root).rglob("*.h")}
    homes: dict[str, tuple[str, Path]] = {}
    for _, scalars, path in sorted(_scalar_headers(headers), key=lambda item: (not item[0], -len(item[1]), item[2])):
        for name, spelling in scalars.items():
            if name in homes and homes[name][0] != spelling:
                held(name, "conflicting project scalar typedef")
            homes.setdefault(name, (spelling, path))
    if not homes:
        return set(), []
    includes = set()
    replacements = []
    for start, end in sorted({(item.start, item.end) for item in parser.declarations}):
        local = Parser(parser.source[start:end])
        if local.peek() != "typedef":
            continue
        local.take("typedef")
        members = local.declaration(typedef=True)
        retained = []
        for member in members:
            if member.name not in homes:
                retained.append(member)
                continue
            spelling, path = homes[member.name]
            if parser.type_name(member.base, member.operations) != spelling or re.search(
                r"\b(?:const|volatile|restrict|__restrict)\b", member.declaration
            ):
                held(member.name, "conflicting draft scalar typedef")
            includes.add(
                next(path.relative_to(root).as_posix() for root in project.include if path.is_relative_to(root))
            )
        if len(retained) != len(members):
            replacements.append((start, end, "\n".join("typedef " + member.declaration for member in retained)))
    return includes, replacements


def _dependencies(fields: tuple[Field, ...]) -> set[str]:
    result: set[str] = set()
    for field in fields:
        match = re.match(r"(?:struct|union) ([A-Za-z_]\w*)", field.type)
        if match and "*" not in field.type:
            result.add(match[1])
        result.update(_dependencies(field.fields))
    return result


def _order_header(text: str, prefix: str) -> str:
    """Order complete declarations without moving guards or duplicating typedefs."""
    parser = Parser(prefix + text)
    records = {record.name: record for record in parser.parse() if record.start >= len(prefix)}
    spans: dict[str, tuple[int, int]] = {}
    for declaration in parser.declarations:
        base = declaration.base
        if isinstance(base, Aggregate) and base.name in records and declaration.start <= base.start < declaration.end:
            spans[base.name] = (declaration.start - len(prefix), declaration.end - len(prefix))
    pending = {name: _dependencies(records[name].fields) & spans.keys() for name in spans}
    ordered = []
    while pending:
        ready = sorted(name for name, dependencies in pending.items() if not dependencies)
        if not ready:
            held(", ".join(sorted(pending)), "cyclic shared-header dependency")
        for name in ready:
            ordered.append(text[slice(*spans[name])])
            del pending[name]
        for dependencies in pending.values():
            dependencies.difference_update(ready)
    slots = sorted(spans.values())
    replacements = [(start, end, value) for (start, end), value in zip(slots, ordered, strict=True)]
    # Standalone aggregate typedefs belong before the ordered definitions.
    # Leaving them in their former slots can hide a pointer alias or name an
    # incomplete by-value dependency after its consumer.
    aliases = []
    for declaration in parser.declarations:
        start, end = declaration.start - len(prefix), declaration.end - len(prefix)
        value = text[start:end]
        if (
            start >= 0
            and isinstance(declaration.base, Aggregate)
            and declaration.base.name in spans
            and not declaration.operations
            and value.lstrip().startswith("typedef ")
            and "{" not in value
        ):
            aliases.append(value)
            replacements.append((start, end, ""))
    position = min((start for start, _, _ in replacements), default=0)
    for start, end, value in sorted(replacements, reverse=True):
        text = text[:start] + value + text[end:]
    if aliases:
        text = text[:position] + "\n".join(aliases) + "\n" + text[position:]
    return text


def fold(
    records: list[Layout], headers: Any, *, versions: tuple[str, ...] | None = None, destination: Path | None = None
) -> list[Edit]:
    """Merge fields into existing include headers, returning edits without writing.

    headers is a project (which supplies include paths and affected VERSIONs), an
    include directory, or an iterable of header paths with explicit versions.
    """
    project = headers if hasattr(headers, "include") else None
    if project is not None:
        versions = tuple(headers.versions)
        paths = sorted({path for root in headers.include for path in Path(root).rglob("*.h")})
        root = None
    else:
        root = Path(headers).resolve() if isinstance(headers, (str, Path)) else None
        paths = sorted(root.rglob("*.h")) if root else sorted(Path(path) for path in headers)
    if not versions:
        held("versions", "affected VERSIONs required")
    if not paths and project is None:
        held("headers", "missing include headers")
    texts = {}
    for path in paths:
        if root is not None and not path.resolve().is_relative_to(root):
            held(str(path), "header must be inside include")
        try:
            texts[path] = path.read_text()
        except OSError as error:
            held(str(path), str(error))
    # Parse the entire index together so cross-header typedef dependencies retain
    # their source order, including forward declarations.
    combined = "\n".join(texts.values())
    parser = Parser(combined)
    existing = parser.parse()
    locations, cursor = {}, 0
    for path, text in texts.items():
        for layout in existing:
            if cursor <= layout.start < cursor + len(text):
                for name in (layout.name, *layout.aliases):
                    locations[name] = (path, layout, cursor)
        cursor += len(text) + 1
    changes: dict[tuple[Path, int, int], dict[str, tuple[Field, list[Field]]]] = {}
    insertions: dict[tuple[Path, int], dict[str, str]] = {}
    requested: dict[tuple[str, str], tuple[int, str, int]] = {}
    additions: dict[str, Layout] = {}
    for record in records:
        selected = next((locations[name] for name in (record.name, *record.aliases) if name in locations), None)
        if selected is None:
            if project is None:
                held(record.name, "missing shared header declaration")
            previous = additions.get(record.name)
            if previous is not None and shared.declaration(previous) != shared.declaration(record):
                held(record.name, "conflicting requested shared declaration")
            additions[record.name] = record
            continue
        path, target, origin = selected
        if record.kind != target.kind:
            held(record.name, "conflicting aggregate kind")
        known = {name: (item, offset) for name, item, offset in _leaves(target.fields)}
        for name, member, offset in _leaves(record.fields):
            if _padding(member):
                continue
            if name in known:
                old, old_offset = known[name]
                if old_offset != offset:
                    held(f"{record.name}.{name}", "conflicting offset")
                if (old.type, old.size, old.bit_offset, old.bit_size) != (
                    member.type,
                    member.size,
                    member.bit_offset,
                    member.bit_size,
                ):
                    held(f"{record.name}.{name}", "conflicting type or extent")
                continue
            request_key = (target.name, name)
            signature = (offset, member.type, member.size)
            if request_key in requested and requested[request_key] != signature:
                held(f"{record.name}.{name}", "conflicting requested field")
            requested[request_key] = signature
            compatible = [
                (old, old_offset)
                for old_name, (old, old_offset) in known.items()
                if old_name.rsplit(".", 1)[-1] == member.name
                and old_offset == offset
                and (old.type, old.size) == (member.type, member.size)
            ]
            if compatible:
                continue
            if "." in name:
                held(f"{record.name}.{name}", "missing nested member requires aggregate declaration")
            containers = [
                item
                for item in target.fields
                if _padding(item) and item.offset <= offset and offset + member.size <= item.offset + item.size
            ]
            if not containers:
                unions = [
                    item
                    for item in target.fields
                    if item.type.startswith("union")
                    and item.offset <= offset
                    and offset + member.size <= item.offset + item.size
                ]
                if not unions:
                    held(f"{record.name}.{name}", "no compatible padding at offset")
                union = unions[0]
                closing = union.start - origin + union.declaration.rfind("}")
                if union.declaration.rfind("}") < 0:
                    held(f"{record.name}.{name}", "union definition is not inline")
                declaration = member.declaration.strip()
                if offset != union.offset:
                    declaration = (
                        f"struct {{ char pad[0x{offset - union.offset:X}]; {declaration} }} view_{member.name};"
                    )
                insertions.setdefault((path, closing), {})[name] = declaration
                continue
            container = containers[0]
            key = (path, container.start - origin, container.end - origin)
            changes.setdefault(key, {}).setdefault(container.name, (container, []))[1].append(member)
    replacements: dict[Path, list[tuple[int, int, str]]] = {}
    for (path, start, end), containers_by_name in changes.items():
        rewritten = {}
        for container_name, (container, members) in containers_by_name.items():
            cursor, lines = container.offset, []
            unique = {member.name: member for member in members}
            for member in sorted(unique.values(), key=lambda item: item.offset):
                if member.offset < cursor:
                    held(member.name, "conflicting overlapping fields")
                if member.offset > cursor:
                    lines.append(f"char pad_{cursor:X}[0x{member.offset - cursor:X}];")
                lines.append(member.declaration.strip())
                cursor = member.offset + member.size
            if cursor < container.offset + container.size:
                lines.append(f"char pad_{cursor:X}[0x{container.offset + container.size - cursor:X}];")
            rewritten[container_name] = "\n    ".join(lines)
        # Replace a complete comma declaration once, preserving every sibling.
        target = next(
            layout
            for candidate, layout, origin in locations.values()
            if candidate == path and any(item.start - origin == start for item in layout.fields)
        )
        origin = locations[target.name][2]
        declarations_for_span = [
            rewritten.get(item.name, item.declaration.strip())
            for item in target.fields
            if item.start - origin == start and item.end - origin == end
        ]
        replacements.setdefault(path, []).append((start, end, "\n    ".join(declarations_for_span)))
    for (path, position), declarations in insertions.items():
        replacements.setdefault(path, []).append(
            (position, position, "\n        " + "\n        ".join(declarations.values()) + "\n    ")
        )
    edits = []
    for path, replacements_for_path in replacements.items():
        before = texts[path]
        after = before
        for start, end, value in sorted(replacements_for_path, reverse=True):
            after = after[:start] + value + after[end:]
        edits.append(Edit(path, before, after, tuple(versions)))
    if additions:
        if project is None:
            held("project", "shared declaration home required")
        path = destination if destination is not None else shared.home(project)
        before = texts.get(path, "")
        before_header = before or (
            f"#ifndef UNBAKE_{path.stem.upper()}_H\n#define UNBAKE_{path.stem.upper()}_H\n"
            + _scalar_include(project, texts, list(additions.values()))
            + "\n#endif\n"
        )
        forward = "".join(
            f"{record.kind} {record.name};\n"
            + "".join(f"typedef {record.kind} {record.name} {alias};\n" for alias in record.aliases)
            for record in sorted(additions.values(), key=lambda item: item.name)
        )
        new_declarations = "\n".join(
            shared.declaration(replace(record, aliases=()))
            for record in sorted(additions.values(), key=lambda item: item.name)
        )
        existing_edit = next((edit for edit in edits if edit.path == path), None)
        if existing_edit is not None:
            edits.remove(existing_edit)
            before_header = existing_edit.after
        # Forward typedefs must precede existing definitions too: an extended
        # aggregate may now use one of the newly promoted types.
        prefix = "\n".join(text for other, text in texts.items() if other != path) + "\n"
        probe_prefix = prefix + forward
        header_parser = Parser(probe_prefix + shared.append(before_header, new_declarations))
        header_records = [
            record
            for record in header_parser.parse()
            if len(probe_prefix) <= record.start < len(probe_prefix) + len(before_header)
        ]
        position = min(
            (record.start - len(probe_prefix) for record in header_records), default=before_header.rfind("#endif")
        )
        if position < 0:
            position = len(before_header)
        if header_records:
            position = min(
                declaration.start - len(probe_prefix)
                for declaration in header_parser.declarations
                if declaration.start <= position + len(probe_prefix) < declaration.end
            )
        before_header = before_header[:position] + forward + before_header[position:]
        after = shared.append(before_header, new_declarations)
        edits.append(Edit(path, before, _order_header(after, prefix), tuple(versions)))
    if edits:
        updated = dict(texts)
        for edit in edits:
            updated[edit.path] = edit.after
        validated = {layout.name: layout for layout in Parser("\n".join(updated.values())).parse()}
        for old_layout in existing:
            new = validated[old_layout.name]
            if new.size != old_layout.size:
                held(old_layout.name, "fold changes aggregate size")
            new_fields = {name: (item, offset) for name, item, offset in _leaves(new.fields)}
            for name, item, offset in _leaves(old_layout.fields):
                if _padding(item):
                    continue
                if name not in new_fields:
                    held(f"{old_layout.name}.{name}", "fold removes existing member")
                replacement, new_offset = new_fields[name]
                if (new_offset, replacement.type, replacement.size, replacement.bit_offset, replacement.bit_size) != (
                    offset,
                    item.type,
                    item.size,
                    item.bit_offset,
                    item.bit_size,
                ):
                    held(f"{old_layout.name}.{name}", "fold changes existing layout")
    return edits
