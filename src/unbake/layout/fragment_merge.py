"""Merge tiny units that are pieces of the function before them back into it.

A splitter that works from a disassembly can cut in the middle of a function: inside a delay slot, after
a conditional return, ahead of a loop tail. The piece it leaves behind is a unit nobody calls that cannot
stand alone: the unit before it runs or branches into it, and its own words read registers nothing set,
branch out of the row, pop a frame it never opened, or end without a return. Such a unit joins the unit
before it and its symbol goes; a small unit that is a complete function stays.
"""

from __future__ import annotations

import struct
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

from unbake.config import Held
from unbake.layout import split
from unbake.layout.dead_prelude import (
    References,
    Span,
    Unit,
    _terminates,
    _transfers,
    data_spans,
    references,
)
from unbake.process import named as cause_named

if TYPE_CHECKING:
    from unbake.config import Project

_BRANCHES = (1, 4, 5, 6, 7, 20, 21, 22, 23)
_ENTRY_GPRS = frozenset((0, 4, 5, 6, 7, 28, 29, 31))
_ENTRY_FPRS = frozenset((12, 13, 14, 15))
_CODE = ("asm", "hasm")
_REASONS = {
    "delay-slot": "the unit before it ends in a jump or branch whose delay slot is this unit's first word",
    "fallthrough": "the unit before it runs into it and its words do not form a function",
    "branch-target": "a branch or jump from the unit before it lands in it and its words do not form a function",
}


@dataclass(frozen=True)
class Fragment:
    name: str
    path: str
    start: int
    address: int
    size: int
    kind: str
    issues: tuple[str, ...]


@dataclass(frozen=True)
class Merge:
    version: str
    parent: str
    path: str
    start: int
    address: int
    size: int
    fragments: tuple[Fragment, ...]

    @property
    def merged_size(self) -> int:
        return self.size + sum(fragment.size for fragment in self.fragments)


def _simm(word: int) -> int:
    value = word & 0xFFFF
    return value - 0x10000 if value & 0x8000 else value


def shape(words: Sequence[int]) -> tuple[str, tuple[str, ...]]:
    """("function" | "fragment" | "stub", reasons): whether the words can be a complete function as they stand.

    A stub opens its frame after code that is not a compiler hoist; that is the prelude rule's, not ours."""
    known, floats = set(_ENTRY_GPRS), set(_ENTRY_FPRS)
    issues: list[str] = []
    frames: list[int] = []
    first_frame: int | None = None
    opened = False
    returns = hi = lo = fpcond = False
    count = len(words)
    for index, word in enumerate(words):
        op, rs, rt, rd = word >> 26, word >> 21 & 31, word >> 16 & 31, word >> 11 & 31
        fn, offset = word & 63, _simm(word)
        reads: list[int] = []
        writes: list[int] = []
        float_reads: list[int] = []
        float_writes: list[int] = []
        at = f"+0x{index * 4:X}"
        if op == 9 and rs == 29 and rt == 29:
            frames.append(offset)
            if offset < 0 and first_frame is None:
                first_frame, opened = index, True
            elif offset > 0 and not opened:
                issues.append(f"frame pop before entry prologue at {at}")
        if op == 0:
            if fn in (0, 2, 3):
                reads, writes = [rt], [rd]
            elif fn in (4, 6, 7, *range(32, 40), *range(42, 48)):
                reads, writes = [rs, rt], [rd]
            elif fn == 8:
                reads, returns = [rs], True
                if index + 1 >= count:
                    issues.append("return/jump is missing its delay slot")
            elif fn == 9:
                reads, writes = [rs], [rd]
                known.update((2, 3))
                floats.update(range(4))
            elif fn in (16, 18):
                writes = [rd]
                if not (hi if fn == 16 else lo):
                    issues.append("entry reads an incoming HI/LO value")
            elif fn in (17, 19):
                reads = [rs]
                hi, lo = hi or fn == 17, lo or fn == 19
            elif 24 <= fn <= 31:
                reads, hi, lo = [rs, rt], True, True
        elif op in (2, 3):
            if index + 1 >= count:
                issues.append("control transfer is missing its delay slot")
            if op == 3:
                known.update((2, 3, 31))
                floats.update(range(4))
            elif index == count - 2 and not opened:
                returns = True
        elif op in _BRANCHES:
            reads = [rs] + ([rt] if op in (4, 5, 20, 21) else [])
            if not 0 <= index + 1 + offset < count:
                issues.append(f"branch leaves the measured row at {at}")
            if index + 1 >= count:
                issues.append("branch is missing its delay slot")
        elif 8 <= op <= 14:
            reads, writes = [rs], [rt]
        elif op == 15:
            writes = [rt]
        elif op == 16:
            if rs in (0, 2):
                writes = [rt]
            elif rs in (4, 6):
                reads = [rt]
        elif op == 17:
            if rs == 0:
                float_reads, writes = [rd], [rt]
            elif rs == 2:
                writes = [rt]
            elif rs == 4:
                reads, float_writes = [rt], [rd]
            elif rs == 6:
                reads = [rt]
            elif rs == 8:
                if not fpcond:
                    issues.append("entry branches on an incoming FPU condition")
                if not 0 <= index + 1 + offset < count:
                    issues.append(f"FPU branch leaves the measured row at {at}")
            elif rs in (16, 17, 20, 21):
                float_reads = [rd] + ([rt] if fn < 4 or fn >= 48 else [])
                float_writes = [word >> 6 & 31] if fn < 48 else []
                fpcond = fpcond or fn >= 48
                if rs == 17:
                    float_reads = sorted({y for x in float_reads for y in (x, x + 1)})
                    float_writes = sorted({y for x in float_writes for y in (x, x + 1)})
        elif op in (32, 33, 34, 35, 36, 37, 38, 39, 48, 52, 55):
            reads, writes = [rs], [rt]
        elif op in (40, 41, 42, 43, 44, 45, 46, 47, 56, 60, 63):
            reads = [rs] if rs == 29 and rt in (*range(16, 24), 30, 31) else [rs, rt]
        elif op in (49, 53):
            reads, float_writes = [rs], [rt] + ([rt + 1] if op == 53 else [])
        elif op in (57, 61):
            reads = [rs]
            float_reads = [] if rs == 29 and rt >= 20 else [rt] + ([rt + 1] if op == 61 else [])
        issues.extend(f"entry consumes undeclared GPR ${r} at {at}" for r in reads if r not in known)
        issues.extend(f"entry consumes undeclared FPR $f{r} at {at}" for r in float_reads if r not in floats)
        known.update(writes)
        floats.update(float_writes)
    if not returns:
        issues.append("row has no complete return or terminal tail transfer")
    if frames and frames[0] > 0 and any(value < 0 for value in frames[1:]):
        return "stub", tuple(issues)
    if first_frame and any(
        "undeclared" in text and int(text.rsplit("+0x", 1)[1], 16) < first_frame * 4 for text in issues
    ):
        return "stub", tuple(issues)
    return ("fragment" if issues else "function"), tuple(dict.fromkeys(issues))


def _words(image: bytes, unit: Unit) -> list[int]:
    raw = image[unit.start : unit.end]
    return [item[0] for item in struct.iter_unpack(">I", raw[: len(raw) // 4 * 4])]


def _called(found: References, position: int) -> bool:
    """Any call, pointer or address pair lands in the unit; jumps and branches are flow, judged separately."""
    return any(kinds - {"j"} for kinds in found.external.get(position, {}).values())


def detect(
    version: str, image: bytes, units: Sequence[Unit], data: Sequence[Span], *, code_kinds: Sequence[str] = _CODE
) -> list[Merge]:
    """Every run of fragments with the unit that owns them, in address order."""
    ordered, found = references(image, units, data)
    merges: list[Merge] = []
    head = 0
    pieces: list[Fragment] = []

    def close() -> None:
        if pieces:
            parent = ordered[head]
            merges.append(
                Merge(
                    version,
                    parent.name,
                    parent.path,
                    parent.start,
                    parent.address,
                    parent.end - parent.start,
                    tuple(pieces),
                )
            )
            pieces.clear()

    for position, unit in enumerate(ordered):
        before = ordered[position - 1] if position else None
        joined = (
            before is not None
            and before.kind in code_kinds
            and unit.kind in code_kinds
            and before.end == unit.start
            and before.address + before.end - before.start == unit.address
            and (unit.end - unit.start) % 4 == 0
        )
        kind = ""
        if joined and before is not None and not _called(found, position):
            sources = found.flows.get(position, set())
            outside = any(not head <= source < position for source in sources)
            if not outside:
                tail = _words(image, before)
                category = shape(_words(image, unit))[0]
                if tail and _transfers(tail[-1]):
                    kind = "delay-slot"
                elif category == "fragment" and not _terminates(tail):
                    kind = "fallthrough"
                elif category == "fragment" and sources:
                    kind = "branch-target"
        if kind:
            pieces.append(
                Fragment(
                    unit.name,
                    unit.path,
                    unit.start,
                    unit.address,
                    unit.end - unit.start,
                    kind,
                    shape(_words(image, unit))[1],
                )
            )
        else:
            close()
            head = position
    close()
    return merges


def census(project: Project, versions: Sequence[str] | None = None) -> list[Merge]:
    """Every proven fragment run in the given versions (default all), largest parent first."""
    found: list[Merge] = []
    for version in list(versions or project.versions):
        configured = project.version(version)
        image = configured.baserom.read_bytes()
        units = [Unit(f.name, f.start, f.end, f.address, f.path, f.kind) for f in split.functions(project, version)]
        found.extend(detect(version, image, units, data_spans(configured, image)))
    return sorted(found, key=lambda item: (-item.merged_size, item.version, item.parent))


def plan(project: Project, found: Sequence[Merge]) -> list[split.Edit]:
    """One layout and one symbols edit per version: fragment rows and their symbols go, the parent keeps the bytes."""
    edits: list[split.Edit] = []
    for version in dict.fromkeys(item.version for item in found):
        configured = project.version(version)
        before, lines, segments = split.layout(configured.split)
        symbols_before, symbols = split.symbols(configured.symbols)
        symbol_lines = symbols_before.splitlines(keepends=True)
        rows = [row for segment in segments for row in segment.rows]
        for item in (m for m in found if m.version == version):
            wanted = [(item.path, item.start), *((piece.path, piece.start) for piece in item.fragments)]
            chosen = [next((r for r in rows if (r.path, r.start) == key), None) for key in wanted]
            if any(row is None or row.kind not in _CODE for row in chosen):
                raise Held(
                    cause_named(
                        item.parent,
                        f"{item.parent}: requires asm rows for the unit and its fragments in VERSION {version}",
                        owner="layout.fragment_merge",
                        stage="boundary",
                    )
                )
            first, *rest = [row for row in chosen if row is not None]
            ordered = first.segment.rows
            index = ordered.index(first)
            if any(row.segment is not first.segment for row in rest) or ordered[index : index + len(chosen)] != [
                first,
                *rest,
            ]:
                raise Held(
                    cause_named(
                        item.parent,
                        f"{item.parent}: its fragments are not the rows that follow it in VERSION {version}",
                        owner="layout.fragment_merge",
                        stage="boundary",
                    )
                )
            for row in rest:
                lines[row.line] = ""
            gone = {piece.address for piece in item.fragments}
            for name, (address, line, _) in symbols.items():
                if address in gone and name != item.parent:
                    symbol_lines[line] = ""
        after = "".join(lines)
        if before != after:
            edits.append(split.Edit(configured.split, before, after, (version,)))
        symbols_after = "".join(symbol_lines)
        if symbols_after != symbols_before:
            edits.append(split.Edit(configured.symbols, symbols_before, symbols_after, (version,)))
    return edits


def describe(item: Merge) -> dict[str, object]:
    return {
        "version": item.version,
        "function": item.parent,
        "rom_offset": item.start,
        "address": item.address,
        "function_bytes": item.size,
        "merged_bytes": item.merged_size,
        "fragments": [
            {"name": p.name, "bytes": p.size, "kind": p.kind, "reasons": list(p.issues)} for p in item.fragments
        ],
    }


def select(found: Sequence[Merge], names: Sequence[str], versions: Sequence[str]) -> list[Merge]:
    chosen = [
        item
        for item in found
        if (not versions or item.version in versions)
        and (not names or item.parent in names or any(piece.name in names for piece in item.fragments))
    ]
    known = {item.parent for item in chosen} | {piece.name for item in chosen for piece in item.fragments}
    missing = sorted(set(names) - known)
    if missing:
        raise Held(
            cause_named(
                "layout.fragment_merge.select",
                "fragment_merge.select: no proven fragment for " + ", ".join(missing),
                owner="layout.fragment_merge",
                stage="boundary",
            )
        )
    return chosen
