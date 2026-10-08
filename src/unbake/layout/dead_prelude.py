"""Find and split unreachable leading bytes of a function whose every reference lands past its entry.

A unit whose callers, call tables and jump targets all point at ENTRY+N (N > 0) has a leading N-byte prelude
that no path reaches: nothing references it, nothing branches into it and the unit before it does not fall
into it. Compiled C cannot emit such bytes, so the unit can never match. The prelude becomes its own data
unit that keeps the original ROM bytes, and the function's symbols move to ENTRY+N.
"""

from __future__ import annotations

import struct
from bisect import bisect_right
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

from unbake.config import Held
from unbake.layout import split
from unbake.layout.rodata_references import collect
from unbake.layout.split_analysis import instruction
from unbake.process import named as cause_named

if TYPE_CHECKING:
    from unbake.config import Project

_BRANCH_OPS = (1, 4, 5, 6, 7, 20, 21, 22, 23)
_DATA_KINDS = ("data", "rodata", "rdata")


@dataclass(frozen=True)
class Unit:
    name: str
    start: int
    end: int
    address: int
    path: str = ""
    kind: str = "asm"
    hints: tuple[tuple[int, str], ...] = ()


@dataclass(frozen=True)
class Span:
    start: int
    end: int
    address: int


@dataclass(frozen=True)
class Prelude:
    version: str
    name: str
    path: str
    start: int
    address: int
    size: int
    function_size: int
    references: tuple[str, ...]
    shape: str = "dead"

    @property
    def new_start(self) -> int:
        return self.start + self.size

    @property
    def new_address(self) -> int:
        return self.address + self.size


def _signed(word: int) -> int:
    value = word & 0xFFFF
    return value - 0x10000 if value & 0x8000 else value


def _terminates(words: Sequence[int]) -> bool:
    """True when the last instruction pair of a unit cannot fall into the next unit."""
    if len(words) < 2:
        return False
    word = words[-2]
    op = word >> 26
    if op == 2 or (op == 0 and word & 63 == 8):
        return True
    return (op == 4 and (word >> 21 & 31) == (word >> 16 & 31)) or (
        op == 1 and (word >> 21 & 31) == 0 and (word >> 16 & 31) in (1, 17)
    )


def _dead(
    version: str,
    image: bytes,
    units: Sequence[Unit],
    unit: Unit,
    found: dict[int, set[str]],
    inside: set[int],
    table: set[int],
) -> Prelude | None:
    """Every outside reference lands at one offset past the start and nothing reaches the bytes before it."""
    if len(found) != 1:
        return None
    ((size, kinds),) = found.items()
    length = unit.end - unit.start
    if size <= 0 or size % 4 or size >= length:
        return None
    if any(offset < size for offset in inside) or any(offset < size for offset in table):
        return None
    words = [item[0] for item in struct.iter_unpack(">I", image[unit.start : unit.start + size])]
    if any(not instruction(word) or _transfers(word) for word in words):
        return None
    before = [
        other for other in units if other.end == unit.start and other.address + other.end - other.start == unit.address
    ]
    if before and before[0].kind in ("asm", "hasm", "c"):
        tail = image[before[0].start : before[0].end]
        if not _terminates([item[0] for item in struct.iter_unpack(">I", tail[: len(tail) // 4 * 4])]):
            return None
    return Prelude(version, unit.name, unit.path, unit.start, unit.address, size, length, tuple(sorted(kinds)))


def _transfers(word: int) -> bool:
    op = word >> 26
    return op in (2, 3) or op in _BRANCH_OPS or (op == 17 and word >> 21 & 31 == 8) or (op == 0 and word & 63 in (8, 9))


_FRAME_REACH = 16
_SCAN = 64
_STORES = (40, 41, 42, 43, 44, 45, 46, 63)
_SAVED = frozenset((*range(16, 24), 30))


def _live(register: int, words: Sequence[int]) -> bool:
    """Whether `register` is read before it is rewritten; control flow it cannot follow counts as a read."""
    from unbake.work.shape import _registers

    for index, word in enumerate(words[:_SCAN]):
        reads, writes, _, _ = _registers(word)
        if register in reads:
            return True
        if _transfers(word):
            delay = words[index + 1] if index + 1 < len(words) else 0
            reads, writes, _, _ = _registers(delay)
            if register in reads:
                return True
            if register in writes:
                return False
            call = word >> 26 == 3 or (word >> 26 == 0 and word & 63 == 9)
            return not call or 4 <= register <= 7
        if register in writes:
            return False
    return True


def _foreign(prefix: Sequence[int], body: Sequence[int]) -> bool:
    """True when some prefix instruction cannot be a compiler hoist ahead of the frame.

    A store through sp before sp moves, a write to a callee-saved register before its save, and a
    result nothing reads are all outside what a compiler emits."""
    from unbake.work.shape import _registers

    for index, word in enumerate(prefix):
        op, base = word >> 26, word >> 21 & 31
        if op in _STORES and base == 29:
            return True
        _, writes, _, _ = _registers(word)
        writes = writes - {0}
        if writes & _SAVED:
            return True
        if writes and not any(_live(register, [*prefix[index + 1 :], *body]) for register in writes):
            return True
    return False


def _stub(
    version: str,
    image: bytes,
    unit: Unit,
    found: dict[int, set[str]],
    inside: set[int],
    table: set[int],
) -> Prelude | None:
    """Instructions ahead of the first stack-frame opening: a compiler never emits them.

    The bytes before `addiu sp,sp,-N` become a unit of their own. Callers that land on them keep the
    stub's symbol; callers that land on the frame opening move with the function."""
    length = unit.end - unit.start
    words = [
        item[0]
        for item in struct.iter_unpack(">I", image[unit.start : unit.start + min(length, (_FRAME_REACH + _SCAN) * 4)])
    ]
    index = next((i for i, word in enumerate(words) if word >> 16 in (0x27BD, 0x67BD) and word & 0x8000), None)
    if not index or index * 4 >= length:
        return None
    size = index * 4
    if any(not instruction(word) or _transfers(word) for word in words[:index]):
        return None
    if not _foreign(words[:index], words[index:]):
        return None
    if any(offset < size for offset in (*inside, *table)) or any(
        0 < offset < size or offset > size for offset in found
    ):
        return None
    return Prelude(
        version,
        unit.name,
        unit.path,
        unit.start,
        unit.address,
        size,
        length,
        ("stub", *sorted({kind for values in found.values() for kind in values})),
        "stub",
    )


def _thunk(version: str, image: bytes, unit: Unit, inside: set[int], found: dict[int, set[str]]) -> Prelude | None:
    """A leading `j`/`jr` elsewhere plus its delay slot, before an entry that opens a compiler frame."""
    length = unit.end - unit.start
    if length <= 8 or length % 4:
        return None
    first, delay, entry = struct.unpack_from(">3I", image, unit.start)
    op = first >> 26
    jump = op == 2 or (op == 0 and first & 63 == 8 and first >> 21 & 31 != 31)
    if not jump or delay >> 26 in (2, 3) or delay >> 26 in _BRANCH_OPS or (delay >> 26 == 0 and delay & 63 in (8, 9)):
        return None
    if op == 2:
        target = ((unit.address + 4) & 0xF0000000) | ((first & 0x3FFFFFF) << 2)
        if unit.address <= target < unit.address + length:
            return None
    if any(offset < 8 for offset in inside):
        return None
    frame = entry >> 16 in (0x27BD, 0x67BD) and entry & 0x8000
    if not frame and 8 not in found:
        return None
    return Prelude(version, unit.name, unit.path, unit.start, unit.address, 8, length, ("thunk",), "thunk")


def detect(
    version: str,
    image: bytes,
    units: Sequence[Unit],
    data: Sequence[Span],
    *,
    code_kinds: Sequence[str] = ("asm", "hasm"),
) -> list[Prelude]:
    """Every unit of `code_kinds` with a provably dead prelude, largest first."""
    ordered = sorted(units, key=lambda unit: unit.address)
    addresses = [unit.address for unit in ordered]

    def owner(value: int) -> int | None:
        index = bisect_right(addresses, value) - 1
        if index < 0 or value >= ordered[index].address + ordered[index].end - ordered[index].start:
            return None
        return index

    external: dict[int, dict[int, set[str]]] = {}
    internal: dict[int, set[int]] = {}
    tables: dict[int, set[int]] = {}

    def note(source: int | None, value: int, kind: str) -> None:
        index = owner(value)
        if index is None:
            return
        offset = value - ordered[index].address
        if source == index:
            internal.setdefault(index, set()).add(offset)
        else:
            external.setdefault(index, {}).setdefault(offset, set()).add(kind)

    for position, unit in enumerate(ordered):
        for offset, kind in unit.hints:
            external.setdefault(position, {}).setdefault(offset, set()).add(kind)
        body = image[unit.start : unit.end]
        words = [item[0] for item in struct.iter_unpack(">I", body[: len(body) // 4 * 4])]
        for index, word in enumerate(words):
            pc = unit.address + index * 4
            op = word >> 26
            if op in (2, 3):
                note(position, ((pc + 4) & 0xF0000000) | ((word & 0x3FFFFFF) << 2), "jal" if op == 3 else "j")
            elif op in _BRANCH_OPS or (op == 17 and (word >> 21 & 31) == 8):
                target = pc + 4 + _signed(word) * 4
                if unit.address <= target < unit.address + len(body):
                    internal.setdefault(position, set()).add(target - unit.address)
        if words:
            for reference in collect(unit.name, body, None, None)[0]:
                if reference.type == "address":
                    note(position, reference.address, "address-pair")
    for span in data:
        raw = image[span.start : span.end]
        values = [item[0] for item in struct.iter_unpack(">I", raw[: len(raw) // 4 * 4])]
        owners = [owner(value) if value >= 0x80000000 else None for value in values]
        for index, value in enumerate(values):
            hit = owners[index]
            if hit is None:
                continue
            offset = value - ordered[hit].address
            neighbours = (owners[index - 1] if index else None, owners[index + 1] if index + 1 < len(owners) else None)
            if hit in neighbours:
                tables.setdefault(hit, set()).add(offset)
            else:
                external.setdefault(hit, {}).setdefault(offset, set()).add("pointer")
    result = []
    for position, unit in enumerate(ordered):
        if unit.kind not in code_kinds:
            continue
        thunk = _thunk(version, image, unit, internal.get(position, set()), external.get(position, {}))
        if thunk:
            result.append(thunk)
            continue
        refs = external.get(position, {})
        shared = internal.get(position, set()), tables.get(position, set())
        found = _dead(version, image, units, unit, refs, *shared) or _stub(version, image, unit, refs, *shared)
        if found:
            result.append(found)
    return sorted(result, key=lambda item: (-item.function_size, item.version, item.name))


def _own_hints(function: split.Function) -> tuple[tuple[int, str], ...]:
    """A function symbol inside the row with none at its start is a recorded entry past the start."""
    if any(offset == 0 for _, offset in function.entries):
        return ()
    return tuple((offset, "symbol") for _, offset in function.entries if offset > 0)


def _image(project: Project, version: str) -> bytes:
    return project.version(version).baserom.read_bytes()


_SIBLING_WORDS = 6
_SIBLING_REACH = 64


def _sibling_entries(
    image: bytes, function: split.Function, images: dict[str, bytes], rows: dict[str, list[split.Function]]
) -> list[tuple[int, str]]:
    """Offsets where another version's same-named function begins inside this version's unit.

    The first words of the sibling must differ from ours and then appear verbatim at +N, so N is the
    sibling's entry in this version's layout."""
    result = []
    length = function.end - function.start
    for version, others in rows.items():
        if version == function.version:
            continue
        match = next((row for row in others if row.name == function.name), None)
        if match is None or match.end - match.start < _SIBLING_WORDS * 4:
            continue
        theirs = images[version][match.start : match.start + _SIBLING_WORDS * 4]
        if image[function.start : function.start + len(theirs)] == theirs:
            continue
        for offset in range(4, _SIBLING_REACH + 1, 4):
            if (
                offset + len(theirs) <= length
                and image[function.start + offset : function.start + offset + len(theirs)] == theirs
            ):
                result.append((offset, "sibling-entry"))
                break
    return result


def census(project: Project, versions: Sequence[str] | None = None) -> list[Prelude]:
    """Every proven dead prelude or leading thunk in the given versions (default all), largest first."""
    chosen = list(versions or project.versions)
    images = {v: _image(project, v) for v in project.versions}
    rows = {v: split.functions(project, v) for v in project.versions}
    sibling: dict[str, list[tuple[str, int]]] = {}
    for v in project.versions:
        for function in rows[v]:
            for offset, _ in _own_hints(function):
                sibling.setdefault(function.name, []).append((v, offset))
    found: list[Prelude] = []
    for version in chosen:
        configured = project.version(version)
        image = images[version]
        units = []
        for f in rows[version]:
            hints = list(_own_hints(f))
            for other, offset in sibling.get(f.name, ()):
                if other == version or offset >= f.end - f.start:
                    continue
                ours = struct.unpack_from(f">{offset // 4}I", image, f.start)
                theirs_row = next(x for x in rows[other] if x.name == f.name)
                theirs = struct.unpack_from(f">{offset // 4}I", images[other], theirs_row.start)
                if [w >> 16 for w in ours] == [w >> 16 for w in theirs]:
                    hints.append((offset, "sibling-symbol"))
            hints.extend(_sibling_entries(image, f, images, rows))
            units.append(Unit(f.name, f.start, f.end, f.address, f.path, f.kind, tuple(hints)))
        _, _, segments = split.layout(configured.split)
        spans = []
        for segment in segments:
            if "vram" not in segment.fields:
                continue
            for row in segment.rows:
                if row.kind in _DATA_KINDS:
                    spans.append(Span(row.start, min(split.end(row), len(image)), split.address(row, configured.split)))
        found.extend(detect(version, image, units, spans))
    return sorted(found, key=lambda item: (-item.function_size, item.version, item.name))


def plan(project: Project, found: Sequence[Prelude]) -> list[split.Edit]:
    """One layout and one symbols edit per version: prelude rows become data, entries and symbols move."""
    edits: list[split.Edit] = []
    for version in dict.fromkeys(item.version for item in found):
        items = [item for item in found if item.version == version]
        configured = project.version(version)
        before, lines, segments = split.layout(configured.split)
        symbols_before, symbols = split.symbols(configured.symbols)
        symbol_lines = symbols_before.splitlines(keepends=True)
        rows = [row for segment in segments for row in segment.rows]
        paths = {row.path for row in rows}
        for item in items:
            matching = [row for row in rows if row.path == item.path and row.start == item.start]
            if len(matching) != 1 or matching[0].kind not in ("asm", "hasm"):
                raise Held(
                    cause_named(
                        item.name,
                        f"{item.name}: requires one asm row at 0x{item.start:X} in VERSION {version}",
                        owner="layout.dead_prelude",
                        stage="boundary",
                    )
                )
            row = matching[0]
            prelude_path = row.path + ("_prelude" if item.shape == "dead" else f"_{item.shape}")
            if prelude_path in paths:
                raise Held(
                    cause_named(
                        item.name,
                        f"{item.name}: prelude path {prelude_path} already exists",
                        owner="layout.dead_prelude",
                        stage="boundary",
                    )
                )
            paths.add(prelude_path)
            template = lines[row.line]
            newline = row.match["newline"] or "\n"
            prelude = split.replace_row(template, row.match, kind="data", path=prelude_path)
            entry = split.replace_row(template, row.match, start=f"0x{item.new_start:06X}")
            lines[row.line] = prelude.rstrip("\r\n") + newline + entry
            for address, index, match in symbols.values():
                if address == item.address:
                    line = symbol_lines[index]
                    symbol_lines[index] = (
                        line[: match.start("address")] + f"0x{item.new_address:08X}" + line[match.end("address") :]
                    )
            if item.shape != "dead":
                thunk = f"{item.name}_{item.shape}"
                if thunk in symbols:
                    raise Held(
                        cause_named(
                            item.name,
                            f"{item.name}: symbol {thunk} already exists",
                            owner="layout.dead_prelude",
                            stage="boundary",
                        )
                    )
                if symbol_lines and not symbol_lines[-1].endswith("\n"):
                    symbol_lines[-1] += newline
                symbol_lines.append(f"{thunk} = 0x{item.address:08X}; // type:func{newline}")
        after = "".join(lines)
        if before != after:
            edits.append(split.Edit(configured.split, before, after, (version,)))
        symbols_after = "".join(symbol_lines)
        if symbols_after != symbols_before:
            edits.append(split.Edit(configured.symbols, symbols_before, symbols_after, (version,)))
    return edits


def describe(item: Prelude) -> dict[str, object]:
    return {
        "version": item.version,
        "function": item.name,
        "rom_offset": item.start,
        "address": item.address,
        "prelude_bytes": item.size,
        "function_bytes": item.function_size,
        "references": list(item.references),
        "shape": item.shape,
    }


def select(found: Sequence[Prelude], names: Sequence[str], versions: Sequence[str]) -> list[Prelude]:
    chosen = [
        item for item in found if (not names or item.name in names) and (not versions or item.version in versions)
    ]
    missing = sorted(set(names) - {item.name for item in chosen})
    if missing:
        raise Held(
            cause_named(
                "layout.dead_prelude.select",
                "dead_prelude.select: no proven dead prelude for " + ", ".join(missing),
                owner="layout.dead_prelude",
                stage="boundary",
            )
        )
    return chosen
