"""Publish missing data placements proved by owning ROM relocation pairs."""

import re
from collections import Counter
from pathlib import Path

from unbake.decomp.needs import Need, SymbolNeed
from unbake.decomp.symbols_edits import resolve
from unbake.layout import split
from unbake.layout.structs_parser import Parser
from unbake.project import build
from unbake.project.config import Held, Policy, Project
from unbake.project_tools.elf import Object


def unresolved(content: str, known: set[str]) -> set[str]:
    """Find used external data declarations in the active preprocessed source."""
    tokens = Parser(content).tokens
    uses = Counter(token[0] for token in tokens if re.fullmatch(r"[A-Za-z_]\w*", token[0]))
    external: set[str] = set()
    index = 0
    while index < len(tokens):
        if tokens[index][0] != "extern":
            index += 1
            continue
        start = index
        while index < len(tokens) and tokens[index][0] != ";":
            index += 1
        if index == len(tokens):
            raise Held("match", "external data declaration: missing semicolon")
        parser = Parser(content[tokens[start].end() : tokens[index].end()])
        members = parser.declaration(typedef=True)
        external.update(
            member.name
            for member in members
            if member.name not in known and (not member.operations or member.operations[0][0] != "function")
        )
        uses.subtract(token[0] for token in tokens[start : index + 1])
        index += 1
    return {name for name in external if uses[name] > 0}


def prepare(project: Project, policy: Policy, function: str, version: str, out: Path) -> list[split.Edit]:
    """Compile only unresolved references through the build's content-keyed cache."""
    source = project.src / f"{function}.c"
    content = build.preprocess_object(project, policy, source, version)
    _, known = split.symbols(project.version(version).symbols)
    if not unresolved(content.decode("utf-8"), set(known)):
        return []
    obj = build.compile_object(project, policy, source, version, out)
    return edits(project, policy, function, version, obj)


def signed(word: int) -> int:
    value = word & 0xFFFF
    return value - 0x10000 if value & 0x8000 else value


def placements(obj: Object, target: bytes, known: set[str], start: int = 0) -> dict[str, int]:
    """Require agreeing, instruction-identical HI16/LO16 references for each name."""
    text = obj.section(".text")
    if text is None:
        raise ValueError(f"{obj.path}: missing .text")
    code = obj.content(text)
    pending: dict[str, list[int]] = {}
    found: dict[str, int] = {}
    for offset, kind, symbol in obj.relocations(text):
        name = symbol["name"]
        if symbol["section"] != 0 or name in known or kind not in (5, 6):
            continue
        relative = offset - start
        if relative < 0 or relative + 4 > len(target):
            raise ValueError(f"{name}: relocation outside owning ROM text")
        word = int.from_bytes(code[offset : offset + 4], "big")
        resident = int.from_bytes(target[relative : relative + 4], "big")
        if word & 0xFFFF0000 != resident & 0xFFFF0000:
            raise ValueError(f"{name}: relocation instruction differs from owning ROM text")
        if kind == 5:
            pending.setdefault(name, []).append(offset)
            continue
        highs = pending.pop(name, [])
        if not highs:
            raise ValueError(f"{name}: missing HI16 relocation")
        for high in highs:
            wanted = int.from_bytes(target[high - start : high - start + 4], "big")
            original = int.from_bytes(code[high : high + 4], "big")
            address = (((wanted & 0xFFFF) << 16) + signed(resident)) & 0xFFFFFFFF
            addend = ((original & 0xFFFF) << 16) + signed(word)
            base = (address - addend) & 0xFFFFFFFF
            if name in found and found[name] != base:
                raise ValueError(f"{name}: conflicting owning ROM data placements")
            found[name] = base
    if pending:
        raise ValueError(f"{', '.join(pending)}: missing LO16 relocation")
    return found


def edits(project: Project, policy: Policy, function: str, version: str, path: Path) -> list[split.Edit]:
    """Emit per-version symbol facts together with the source publication."""
    rows = [row for row in split.functions(project, version) if function in row.aliases]
    if len(rows) != 1:
        raise Held("match", f"{function}: VERSION {version}: expected one owning text row")
    _, known = split.symbols(project.version(version).symbols)
    try:
        obj = Object(path)
        entries = [
            symbol
            for table in obj.symbols.values()
            for symbol in table
            if symbol["name"] == function and symbol["section"] == obj.section(".text")
        ]
        if len(entries) != 1:
            raise ValueError(f"{function}: expected one compiled text entry")
        found = placements(obj, split.words(project, rows[0]), set(known), entries[0]["value"])
    except (OSError, ValueError) as error:
        raise Held("match", f"{function}: VERSION {version}: data placement: {error}") from error
    pending: list[Need] = [
        SymbolNeed(version, name, address, 0, "data", "address", 0, "owning ROM HI16/LO16 pairs")
        for name, address in found.items()
    ]
    return resolve(pending, project, policy) if pending else []
