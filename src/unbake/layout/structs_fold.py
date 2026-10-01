"""Fold proved fields into shared include declarations."""

from __future__ import annotations

import os
import re
import shutil
import tempfile
from collections.abc import Iterator
from dataclasses import replace
from itertools import pairwise
from pathlib import Path
from typing import Any

from unbake.layout import shared
from unbake.layout.split import Edit
from unbake.layout.structs import Field, Layout, held
from unbake.layout.structs_parser import Parser
from unbake.layout.structs_types import SCALARS, Aggregate
from unbake.project.config import Held, Project, load_policy


def _leaves(fields: tuple[Field, ...], offset: int = 0, prefix: str = "") -> Iterator[tuple[str, Field, int]]:
    for item in fields:
        name = f"{prefix}.{item.name}".strip(".")
        if item.fields and not item.extent:
            yield from _leaves(item.fields, offset + item.offset, name)
        else:
            yield name, item, offset + item.offset


def _nodes(fields: tuple[Field, ...], offset: int = 0) -> Iterator[tuple[Field, int]]:
    for item in fields:
        yield item, offset + item.offset
        if item.fields and not item.extent:
            yield from _nodes(item.fields, offset + item.offset)


def _padding(item: Field) -> bool:
    return bool(re.match(r"^(?:pad|padding)\w*$", item.name)) and item.type.split("[", 1)[0] in (
        "char",
        "u8",
        "s8",
        "unsigned char",
    )


def _signature(item: Field, offset: int) -> tuple[int, str, int, int | None, int | None]:
    return offset, item.type, item.size, item.bit_offset, item.bit_size


def _subset(record: Layout, target: Layout) -> bool:
    """A union may expose several views of the same byte range."""
    known = {name: (item, offset) for name, item, offset in _leaves(target.fields)}
    return all(
        _padding(member)
        or (
            _signature(*known[name]) == _signature(member, offset)
            if name in known
            else any(
                not _padding(old) and _signature(old, old_offset) == _signature(member, offset)
                for old, old_offset in known.values()
            )
        )
        for name, member, offset in _leaves(record.fields)
    )


def _sdk_gfx(record: Layout) -> bool:
    """Recognize the SDK display-list union by its complete word view."""
    if record.kind != "union" or "Gfx" not in (record.name, *record.aliases) or record.size != 8:
        return False
    words = {name: (field, offset) for name, field, offset in _leaves(record.fields)}
    return all(
        name in words and words[name][0].size == 4 and words[name][1] == offset
        for name, offset in (("words.w0", 0), ("words.w1", 4))
    )


def _conflict(
    record: Layout,
    name: str,
    member: Field,
    offset: int,
    target: Layout,
    path: Path,
    old_name: str,
    old: Field,
    old_offset: int,
) -> None:
    source = record.source if re.fullmatch(r"[^\n;{}]+\.h", record.source) else "draft header"
    held(
        f"{record.name}.{name} ({source})",
        f"conflicts with {target.name}.{old_name} ({path}): "
        f"offset/type/size {_signature(member, offset)} versus {_signature(old, old_offset)}",
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


def _order_header(text: str, prefix: str, before: str = "") -> str:
    """Order complete declarations without moving guards or duplicating typedefs."""
    parser = Parser(prefix + text)
    records = {record.name: record for record in parser.parse() if record.start >= len(prefix)}
    spans: dict[str, tuple[int, int]] = {}
    for declaration in parser.declarations:
        base = declaration.base
        if isinstance(base, Aggregate) and base.name in records and declaration.start <= base.start < declaration.end:
            spans[base.name] = (declaration.start - len(prefix), declaration.end - len(prefix))
    pending = {name: _dependencies(records[name].fields) & spans.keys() for name in spans}
    # Existing definitions retain their order and every surrounding declaration.
    # Only newly added definitions may move to satisfy a by-value dependency.
    existing = {record.name for record in Parser(prefix + before).parse() if record.start >= len(prefix)}
    protected = sorted((span[0], name) for name, span in spans.items() if name in existing)
    for (_, previous), (_, following) in pairwise(protected):
        pending[following].add(previous)
    ordered = []
    while pending:
        ready = sorted(name for name, dependencies in pending.items() if not dependencies)
        if not ready:
            held(", ".join(sorted(pending)), "cyclic shared-header dependency")
        for name in ready:
            ordered.append(name)
            del pending[name]
        for dependencies in pending.values():
            dependencies.difference_update(ready)
    replacements = [(start, end, "") for name, (start, end) in spans.items() if name not in existing]
    additions: list[str] = []
    for name in ordered:
        if name in existing:
            if additions:
                position = spans[name][0]
                replacements.append((position, position, "\n".join(additions) + "\n"))
                additions = []
        else:
            additions.append(text[slice(*spans[name])])
    if additions:
        position = text.rfind("#endif")
        if position < 0:
            position = len(text)
        replacements.append((position, position, "\n".join(additions) + "\n"))
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
            and declaration.base.name not in existing
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
    locations: dict[str, list[tuple[Path, Layout, int]]] = {}
    identities: list[tuple[Path, Layout]] = []
    cursor = 0
    for path, text in texts.items():
        for layout in existing:
            if cursor <= layout.start < cursor + len(text):
                identities.append((path, layout))
                for name in (layout.name, *layout.aliases):
                    locations.setdefault(name, []).append((path, layout, cursor))
        cursor += len(text) + 1
    changes: dict[tuple[Path, int, int], dict[str, tuple[Field, list[Field]]]] = {}
    insertions: dict[tuple[Path, int], dict[str, str]] = {}
    gaps: dict[tuple[Path, int], tuple[int, dict[str, tuple[Field, int]]]] = {}
    requested: dict[tuple[str, str], tuple[int, str, int]] = {}
    additions: dict[str, Layout] = {}
    for record in records:
        candidates = [location for name in (record.name, *record.aliases) for location in locations.get(name, [])]
        candidates.sort(key=lambda location: location[0].name != "n64sdk.h")
        selected = next((location for location in candidates if _subset(record, location[1])), None)
        if selected is not None:
            continue
        selected = next(iter(candidates), None)
        if selected is None:
            if project is None:
                held(record.name, "missing shared header declaration")
            previous = additions.get(record.name)
            if previous is not None and shared.declaration(previous) != shared.declaration(record):
                held(record.name, "conflicting requested shared declaration")
            additions[record.name] = record
            continue
        path, target, origin = selected
        if _sdk_gfx(target):
            held(record.name, f"inferred view conflicts with SDK Gfx union in {path}; preserve its existing members")
        known = {name: (item, offset) for name, item, offset in _leaves(target.fields)}
        for name, member, offset in _leaves(record.fields):
            if _padding(member):
                continue
            if name in known:
                old, old_offset = known[name]
                if _signature(old, old_offset) != _signature(member, offset):
                    _conflict(record, name, member, offset, target, path, name, old, old_offset)
                continue
            request_key = (target.name, name)
            signature = (offset, member.type, member.size)
            if request_key in requested and requested[request_key] != signature:
                held(f"{record.name}.{name}", "conflicting requested field")
            requested[request_key] = signature
            compatible = [
                (old, old_offset)
                for old_name, (old, old_offset) in known.items()
                if not _padding(old) and _signature(old, old_offset) == _signature(member, offset)
            ]
            if compatible:
                continue
            overlaps = [
                (old_name, old, old_offset)
                for old_name, (old, old_offset) in known.items()
                if not _padding(old) and old_offset < offset + member.size and offset < old_offset + old.size
            ]
            if overlaps and not any(
                item.type.startswith("union")
                and item.offset <= offset
                and offset + member.size <= item.offset + item.size
                for item in target.fields
            ):
                old_name, old, old_offset = overlaps[0]
                _conflict(record, name, member, offset, target, path, old_name, old, old_offset)
            if path.name == "n64sdk.h":
                # SDK typedefs use anonymous aggregates. Give an extended
                # inferred view its own tag in the shared home; the SDK
                # typedef and all its views remain unchanged.
                if re.match(r"(?:struct|union)\s*\{", combined[target.start : target.body_start]):
                    additions[record.name] = replace(record, aliases=())
                    continue
                held(f"{record.name}.{name} (draft header)", f"cannot extend read-only SDK tag in {path}")
            containers = [
                (item, absolute)
                for item, absolute in _nodes(target.fields)
                if _padding(item) and absolute <= offset and offset + member.size <= absolute + item.size
            ]
            if not containers:
                if "." in name:
                    held(f"{record.name}.{name}", f"overlapping aggregate in {path}")
                unions = [
                    item
                    for item in target.fields
                    if item.type.startswith("union")
                    and item.offset <= offset
                    and offset + member.size <= item.offset + item.size
                ]
                if not unions:
                    # Implicit alignment gaps and the tail are unused too.
                    # Insert after the preceding declaration, spelling any
                    # gap explicitly so the inferred offset remains exact.
                    preceding = [item for item in target.fields if item.offset + item.size <= offset]
                    following = [item for item in target.fields if item.offset >= offset + member.size]
                    start_offset = max((item.offset + item.size for item in preceding), default=0)
                    if len(preceding) + len(following) != len(target.fields):
                        held(f"{record.name}.{name}", f"overlapping aggregate in {path}")
                    position = min((item.start for item in following), default=target.body_end) - origin
                    gaps.setdefault((path, position), (start_offset, {}))[1][name] = (member, offset)
                    continue
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
            container, absolute = containers[0]
            container_path, container_origin = next(
                (candidate, candidate_origin)
                for entries in locations.values()
                for candidate, layout, candidate_origin in entries
                if candidate_origin <= container.start < candidate_origin + len(texts[candidate])
            )
            if container_path.name == "n64sdk.h":
                held(f"{record.name}.{name} (draft header)", f"cannot extend read-only SDK type in {container_path}")
            key = (container_path, container.start - container_origin, container.end - container_origin)
            changes.setdefault(key, {}).setdefault(container.name, (replace(container, offset=absolute), []))[1].append(
                replace(member, offset=offset)
            )
    replacements: dict[Path, list[tuple[int, int, str]]] = {}
    for gap_key, (cursor, gap_members) in gaps.items():
        lines = []
        for member, offset in sorted(gap_members.values(), key=lambda entry: entry[1]):
            if offset < cursor:
                held(member.name, "conflicting overlapping requested fields")
            if cursor < offset:
                lines.append(f"char pad_{cursor:X}[0x{offset - cursor:X}];")
            lines.append(member.declaration.strip())
            cursor = offset + member.size
        insertions.setdefault(gap_key, {})["gap"] = "\n    ".join(lines)
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
            for entries in locations.values()
            for candidate, layout, origin in entries
            if candidate == path and any(item.start - origin == start for item, _ in _nodes(layout.fields))
        )
        origin = next(origin for candidate, layout, origin in locations[target.name] if layout is target)
        declarations_for_span = [
            rewritten.get(item.name, item.declaration.strip())
            for item, _ in _nodes(target.fields)
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
        edits.append(Edit(path, before, _order_header(after, prefix, before), tuple(versions)))
    if edits:
        updated = dict(texts)
        for edit in edits:
            updated[edit.path] = edit.after
        parsed = Parser("\n".join(updated.values())).parse()
        # Typedef names can occur in independent headers (SDK and inferred
        # views). Preserve each declaration, rather than overwriting by name.
        validated: dict[tuple[Path, str], list[Layout]] = {}
        cursor = 0
        for path, text in updated.items():
            for layout in parsed:
                if cursor <= layout.start < cursor + len(text):
                    validated.setdefault((path, layout.name), []).append(layout)
            cursor += len(text) + 1
        for path, old_layout in identities:
            if updated[path] == texts[path]:
                # An unchanged declaration cannot lose members. Other headers
                # may introduce a same-named inferred tag; the global parser
                # index does not track C scopes. The physical include proof
                # below still verifies every affected consumer.
                continue
            new = validated[(path, old_layout.name)].pop(0)
            if new.size < old_layout.size:
                held(old_layout.name, "fold shrinks aggregate size")
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
        if project is not None and getattr(project, "src", None) is not None and Path(project.src).is_dir():
            if not isinstance(project, Project):
                held(
                    ", ".join(str(edit.path) for edit in edits),
                    "project compiler context required for header compile proof",
                )
            _prove_includers(project, edits)
    return edits


def _prove_includers(project: Project, edits: list[Edit]) -> None:
    label = ", ".join(str(edit.path) for edit in edits)
    try:
        _compile_includers(project, edits)
    except Held as error:
        if error.reason.startswith(label):
            raise
        held(label, error.reason)
    except (OSError, ValueError) as error:
        held(label, f"header compile proof unavailable: {error}")


def _compile_includers(project: Project, edits: list[Edit]) -> None:
    """Compile all possible includers in a physical overlay before returning edits.

    Conditional literal includes are deliberately overapproximated. Computed
    includes cannot be proved by this scan and are refused before any write.
    Both matching and NON_MATCHING source branches are checked in every VERSION.
    """
    from unbake.decomp.explain import _absolute_includes
    from unbake.decomp.trial_compile import run_tool
    from unbake.project import makefile, toolchain
    from unbake.project_tools.sn64_cc import partition_flags

    changed = {edit.path.resolve() for edit in edits}
    graph: dict[Path, set[Path]] = {}
    recipe = makefile.recipe(project)
    configured_flags = (
        *(compiler.cflags for compiler in project.compilers.values()),
        *recipe.unit_cflags.values(),
    )
    search = list(project.include)
    for configured in configured_flags:
        flag_iter = iter(configured)
        for option in flag_iter:
            value = (
                next(flag_iter, "")
                if option in ("-I", "-isystem", "-iquote")
                else option[2:]
                if option.startswith("-I")
                else ""
            )
            if value:
                path = Path(value)
                search.append(path if path.is_absolute() else project.root / path)

    def dependencies(path: Path) -> set[Path]:
        path = path.resolve()
        if path in graph:
            return graph[path]
        graph[path] = set()
        text = re.sub(r"/\*.*?\*/|//[^\n]*", "", path.read_text(), flags=re.S).replace("\\\n", "")
        for directive in re.findall(r"^\s*#\s*include\b\s*([^\n]+)", text, re.M):
            literal = re.fullmatch(r'["<]([^">]+)[">]\s*', directive)
            if literal is None:
                held(str(path), f"cannot prove header includers for computed include {directive}")
            if Path(literal[1]).is_absolute():
                held(str(path), f"cannot isolate absolute include {literal[1]} for header compile proof")
            candidates = [path.parent / literal[1], *(root / literal[1] for root in search)]
            # Include flags and conditional headers can choose different homes.
            # Scan all known candidates so the proof cannot miss an includer.
            for candidate in candidates:
                if candidate.is_file():
                    target = candidate.resolve()
                    if not target.is_relative_to(project.root):
                        held(str(path), f"cannot isolate external include {target} for header compile proof")
                    graph[path].add(target)
                    dependencies(target)
        return graph[path]

    def affected(path: Path, visited: set[Path]) -> bool:
        path = path.resolve()
        if path in changed:
            return True
        if path in visited:
            return False
        visited.add(path)
        return any(affected(child, visited) for child in dependencies(path))

    forced_config = any("-include" in flags for flags in configured_flags)
    sources = sorted(project.src.rglob("*.c"))
    includers: list[tuple[Path, str, list[str]]] = []
    for source in sources:
        direct = affected(source, set())
        if not direct and not forced_config:
            continue
        for version in project.versions:
            flags = _absolute_includes(project, makefile.flags(project, version, source))
            forced = [Path(flags[index + 1]) for index, flag in enumerate(flags) if flag == "-include"]
            if direct or any(affected(path, set()) for path in forced):
                includers.append((source, version, flags))
    if not includers:
        return
    label = ", ".join(str(edit.path) for edit in edits)
    policy = load_policy()
    # TMPDIR must be explicit and outside the project; never stage a proposed
    # header over the live one, even transiently.
    temporary_root = os.environ.get("TMPDIR")
    if not temporary_root or Path(temporary_root).resolve().is_relative_to(project.root):
        held(label, "header compile proof requires TMPDIR outside project.root")
    try:
        with tempfile.TemporaryDirectory(prefix="structs-proof-", dir=temporary_root) as temporary:
            overlay = Path(temporary)
            for root in (project.src, *project.include):
                shutil.copytree(root, overlay / root.relative_to(project.root), dirs_exist_ok=True)
            for dependency in graph:
                destination = overlay / dependency.relative_to(project.root)
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(dependency, destination)
            for edit in edits:
                destination = overlay / edit.path.relative_to(project.root)
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_text(edit.after)
            verified = set()
            for index, (source, version, flags) in enumerate(includers):
                compiler = project.compiler_for(source)
                if compiler.id not in verified:
                    toolchain.verify(project.tools / compiler.id, toolchain.specification(compiler.id))
                    verified.add(compiler.id)
                if compiler.kind not in ("sn64", "ido"):
                    held(label, f"cannot prove {source} VERSION {version}: unsupported compiler {compiler.kind}")
                flags = [flag.replace(str(project.root) + "/", str(overlay) + "/") for flag in flags]
                for nonmatching in (False, True):
                    options = [*flags, "-DNON_MATCHING=1"] if nonmatching else [*flags, "-UNON_MATCHING"]
                    staged = overlay / source.relative_to(project.root)
                    output = overlay / f"proof-{index}-{int(nonmatching)}.o"
                    try:
                        if compiler.kind == "sn64":
                            cppflags, codeflags = partition_flags(options)
                            cpp = makefile.host_executable(policy, recipe.cpp or "policy:cpp", "cpp")
                            expanded = run_tool([cpp, *recipe.cppflags, *cppflags, str(staged)], overlay, "structs")
                            preprocessed = output.with_suffix(".i")
                            preprocessed.write_text(expanded)
                            run_tool(
                                [str(compiler.cc), "-quiet", *codeflags, str(preprocessed), "-o", str(output)],
                                overlay,
                                "structs",
                            )
                        else:
                            run_tool(
                                [str(compiler.cc), *options, "-c", str(staged), "-o", str(output)], overlay, "structs"
                            )
                    except Held as error:
                        held(
                            label,
                            f"header compile proof failed for {source} VERSION {version} "
                            f"NON_MATCHING={int(nonmatching)}: {error.reason}",
                        )
    except (OSError, ValueError) as error:
        held(label, f"header compile proof unavailable: {error}")
