"""Split a unit that holds two functions where the first one ends and the second one begins.

Inside one unit a terminator (`jr ra` with its delay slot, `eret`, or a `j` to outside the unit) followed
by a valid function entry cuts the unit in two. An entry is a stack-frame opening (`addiu sp,sp,-N`) or
an address that a call, jump, pointer or sibling-version symbol reaches from elsewhere. Words reached
only by falling through, by a branch or jump from the other part, or as a jump-table label are fragments:
`boundary merge` joins those, so this rule leaves them alone. Dead preludes and thunks are `boundary
prelude`'s and are not cut here.
"""

from __future__ import annotations

import struct
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from unbake.config import Held
from unbake.layout import dead_prelude, split
from unbake.layout.dead_prelude import Span, Unit
from unbake.process import named as cause_named

if TYPE_CHECKING:
    from unbake.config import Project

_ERET = 0x42000018
_MIN_PART = 8
_BRANCHES = (1, 4, 5, 6, 7, 20, 21, 22, 23)


@dataclass(frozen=True)
class Cut:
    version: str
    parent: str
    path: str
    start: int
    address: int
    offset: int
    unit_size: int
    ending: str  # how the first function ends: eret, return, jump
    entry: str  # why the next word starts a function: prologue, referenced

    @property
    def new_start(self) -> int:
        return self.start + self.offset

    @property
    def new_address(self) -> int:
        return self.address + self.offset


def _signed(word: int) -> int:
    value = word & 0xFFFF
    return value - 0x10000 if value & 0x8000 else value


def _ending(words: Sequence[int], index: int, address: int) -> tuple[str, int] | None:
    """(kind, index of the next word) when the word at `index` ends a function."""
    word = words[index]
    op = word >> 26
    if word == _ERET:
        return "eret", index + 1
    if op == 0 and word & 63 == 8 and word >> 21 & 31 == 31:
        return "return", index + 2
    if op == 2:
        target = ((address + index * 4 + 4) & 0xF0000000) | ((word & 0x3FFFFFF) << 2)
        if not address <= target < address + len(words) * 4:
            return "jump", index + 2
    return None


def _edges(words: Sequence[int], address: int) -> tuple[list[tuple[int, int]], set[int]]:
    """Branch and jump edges (source, target) inside the unit and the targets of its own `jal`s, as word indices."""
    edges, calls = [], set()
    size = len(words)
    for index, word in enumerate(words):
        op = word >> 26
        if op in _BRANCHES or (op == 17 and word >> 21 & 31 == 8):
            target = index + 1 + _signed(word)
        elif op in (2, 3):
            absolute = ((address + index * 4 + 4) & 0xF0000000) | ((word & 0x3FFFFFF) << 2)
            target = (absolute - address) // 4
        else:
            continue
        if 0 <= target < size:
            if op == 3:
                calls.add(target)
            else:
                edges.append((index, target))
    return edges, calls


def detect(
    version: str,
    image: bytes,
    units: Sequence[Unit],
    data: Sequence[Span],
    *,
    code_kinds: Sequence[str] = ("asm", "hasm"),
) -> list[Cut]:
    """Every cut of a unit of `code_kinds` into functions, largest unit first."""
    ordered, found = dead_prelude.references(image, units, data)
    prelude = {item.name for item in dead_prelude.detect(version, image, units, data, code_kinds=code_kinds)}
    result: list[Cut] = []
    for position, unit in enumerate(ordered):
        length = unit.end - unit.start
        if unit.kind not in code_kinds or unit.name in prelude or length < 2 * _MIN_PART or length % 4:
            continue
        words = [item[0] for item in struct.iter_unpack(">I", image[unit.start : unit.end])]
        edges, calls = _edges(words, unit.address)
        outside = found.external.get(position, {})
        table = {offset // 4 for offset in found.tables.get(position, set())}
        for index in range(len(words)):
            ending = _ending(words, index, unit.address)
            if ending is None:
                continue
            kind, cut = ending
            if cut * 4 < _MIN_PART or (len(words) - cut) * 4 < _MIN_PART:
                continue
            word = words[cut]
            prologue = word >> 16 in (0x27BD, 0x67BD) and bool(word & 0x8000)
            referenced = bool(outside.get(cut * 4)) or cut in calls
            if not prologue and not referenced:
                continue
            if any((source < cut) != (target < cut) for source, target in edges):
                continue
            if cut in table or (any(t < cut for t in table) and any(t >= cut for t in table)):
                continue
            result.append(
                Cut(
                    version,
                    unit.name,
                    unit.path,
                    unit.start,
                    unit.address,
                    cut * 4,
                    length,
                    kind,
                    "prologue" if prologue else "referenced",
                )
            )
    return sorted(result, key=lambda item: (-item.unit_size, item.version, item.parent, item.offset))


def census(project: Project, versions: Sequence[str] | None = None) -> list[Cut]:
    found: list[Cut] = []
    for version, (image, units, spans) in dead_prelude.version_units(project, versions).items():
        found.extend(detect(version, image, units, spans))
    return sorted(found, key=lambda item: (-item.unit_size, item.version, item.parent, item.offset))


def counts(found: Sequence[Cut]) -> list[str]:
    ending = Counter(item.ending for item in found)
    entry = Counter(item.entry for item in found)
    return (
        [
            f"splits: {len(found)}; by end "
            + ", ".join(f"{key}={ending[key]}" for key in sorted(ending))
            + "; by entry "
            + ", ".join(f"{key}={entry[key]}" for key in sorted(entry))
        ]
        if found
        else ["splits: 0"]
    )


def plan(project: Project, found: Sequence[Cut]) -> list[split.Edit]:
    """One layout and one symbols edit per version: a row per cut, named by its address unless a symbol is there."""
    edits: list[split.Edit] = []
    for version in dict.fromkeys(item.version for item in found):
        items = sorted((item for item in found if item.version == version), key=lambda item: item.new_start)
        configured = project.version(version)
        before, lines, segments = split.layout(configured.split)
        symbols_before, symbols = split.symbols(configured.symbols)
        symbol_lines = symbols_before.splitlines(keepends=True)
        by_address = {address: name for name, (address, _, _) in symbols.items()}
        rows = [row for segment in segments for row in segment.rows]
        paths = {row.path for row in rows}
        newline = "\r\n" if "\r\n" in before else "\n"
        pending: dict[int, list[str]] = {}
        for item in items:
            matching = [row for row in rows if row.path == item.path and row.start == item.start]
            if len(matching) != 1 or matching[0].kind != "asm":
                raise _held(item, f"requires one asm row at 0x{item.start:X} in VERSION {version}")
            row = matching[0]
            name = by_address.get(item.new_address, f"func_{item.new_address:08X}")
            directory = str(Path(row.path).parent)
            target = name if directory == "." else f"{directory}/{name}"
            if target in paths:
                raise _held(item, f"function {name} already has a row")
            paths.add(target)
            template = lines[row.line] if row.match["newline"] else lines[row.line] + newline
            pending.setdefault(row.line, []).append(
                split.replace_row(template, row.match, start=f"0x{item.new_start:06X}", path=target)
            )
            if item.new_address not in by_address:
                by_address[item.new_address] = name
                if symbol_lines and not symbol_lines[-1].endswith("\n"):
                    symbol_lines[-1] += newline
                symbol_lines.append(f"{name} = 0x{item.new_address:08X}; // type:func{newline}")
        for line, added in pending.items():
            current = lines[line]
            tail = "" if current.endswith("\n") else newline
            lines[line] = current + tail + "".join(added)
            if not current.endswith("\n"):
                lines[line] = lines[line].removesuffix(newline)
        after = "".join(lines)
        if before != after:
            edits.append(split.Edit(configured.split, before, after, (version,)))
        symbols_after = "".join(symbol_lines)
        if symbols_after != symbols_before:
            edits.append(split.Edit(configured.symbols, symbols_before, symbols_after, (version,)))
    return edits


def _held(item: Cut, message: str) -> Held:
    return Held(cause_named(item.parent, f"{item.parent}: {message}", owner="layout.function_split", stage="boundary"))


def select(found: Sequence[Cut], names: Sequence[str], versions: Sequence[str]) -> list[Cut]:
    chosen = [
        item for item in found if (not names or item.parent in names) and (not versions or item.version in versions)
    ]
    missing = sorted(set(names) - {item.parent for item in chosen})
    if missing:
        raise Held(
            cause_named(
                "layout.function_split.select",
                "function_split.select: no proven function boundary in " + ", ".join(missing),
                owner="layout.function_split",
                stage="boundary",
            )
        )
    return chosen
