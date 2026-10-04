"""Infer layout groups (modules) from evidence every compiled N64 ROM carries.

Same-object evidence joins adjacent functions; object padding cuts them; the layout cap packs what is left.
- rodata: two functions that load the same resident constant. Compilers merge equal literals, strings and
  tables only inside one object, so the functions between them belong to that object.
- callee: a function whose callers all lie within 16 functions of it is that window's static helper.
- padding: zero words after a function's return that end on a 16-byte boundary are the end of an object.
- version: a function that only some versions hold stays with its neighbour while the module is under the cap.
A boundary is a cut only when every version holding both sides agrees; a join from any version holds.
Joins never cross a cut, and no single evidence item reaches further than the cap. A direct join keeps its run whole,
but overlapping joins never chain a run past the cap: such a chain is cut at its weakest boundary (fewest shared
references in any one version) until each piece is within the cap or lies inside one direct join. The cap then
packs what evidence leaves unjoined. Only `default` groups
are inferred; inferred, proven and hypothesis groups stay as they are, so the result is stable and a later proof
can confirm or split it.
"""

from __future__ import annotations

import itertools
from collections import defaultdict
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from unbake.config import Project
from unbake.decomp.symbols import references
from unbake.layout import split
from unbake.layout.map import Group, Map, Member

_RETURN = 0x03E00008
_OBJECT_ALIGN = 16
_CALLEE_REACH = 16
"""A static helper sits near all of its callers. On BattleTanx, where padding marks every object end, callee
windows of at most 16 functions cross an object end in 45 of 386 cases; wider windows cross in 438 of 721."""
_WEAK = {"version"}
"""A weak join holds only while the module is under the cap."""


@dataclass(frozen=True)
class Function:
    name: str
    segment: str
    start: int
    end: int
    address: int


@dataclass
class Evidence:
    """One version's evidence: join intervals by first and last name, and cut pairs."""

    joins: list[tuple[str, str, str]] = field(default_factory=list)
    cuts: set[tuple[str, str]] = field(default_factory=set)


def padding(words: Sequence[int]) -> int:
    """Zero words after the return and its delay slot at the end of a function."""
    zeros = 0
    while zeros < len(words) and words[-1 - zeros] == 0:
        zeros += 1
    if zeros == len(words):
        return 0
    return zeros - 1 if words[-1 - zeros] == _RETURN else zeros


def version_evidence(
    functions: Sequence[Function],
    words: Callable[[Function], list[int]],
    constants: Callable[[int], bool],
    gp: int | None,
    cap: int,
) -> Evidence:
    """Evidence from one version's functions (in ROM order), code words and resident constant ranges."""
    order = sorted(functions, key=lambda f: f.start)
    position = {f.name: index for index, f in enumerate(order)}
    by_address = {f.address: f for f in order}
    evidence = Evidence()
    loads: dict[int, set[str]] = defaultdict(set)
    callers: dict[str, set[str]] = defaultdict(set)
    for function in order:
        code = words(function)
        for reference in references(code, gp):
            if constants(reference.address):
                loads[reference.address].add(function.name)
        for index, word in enumerate(code):
            if word >> 26 == 3:
                pc = function.address + index * 4
                target = by_address.get((pc & 0xF0000000) | ((word & 0x03FFFFFF) << 2))
                if target is not None and target.name != function.name:
                    callers[target.name].add(function.name)

    def join(names: set[str], signal: str, reach: int) -> None:
        if len({by_name[name].segment for name in names}) != 1:
            return
        first, last = min(names, key=position.__getitem__), max(names, key=position.__getitem__)
        if position[last] - position[first] < reach:
            evidence.joins.append((first, last, signal))

    by_name = {f.name: f for f in order}
    for address in sorted(loads):
        if len(loads[address]) > 1:
            join(loads[address], "rodata", cap)
    for callee in sorted(callers):
        join(callers[callee] | {callee}, "callee", min(cap, _CALLEE_REACH))
    for left, right in itertools.pairwise(order):
        if (
            left.segment == right.segment
            and left.end == right.start
            and right.address % _OBJECT_ALIGN == 0
            and padding(words(left)) > 0
        ):
            evidence.cuts.add((left.name, right.name))
    return evidence


def _cut_chains(
    joined: list[set[str]], cut: list[str | None], weight: list[int], direct: set[tuple[int, int]], cap: int
) -> None:
    """Cut every run chained by overlapping joins past the cap at its weakest boundary, nearest the middle on ties,
    until each piece is within the cap or lies inside one direct join (member indices LOW..HIGH)."""
    pending: list[tuple[int, int]] = []
    start = 0
    for position in range(len(joined) + 1):
        if position == len(joined) or cut[position] or not joined[position] - _WEAK:
            pending.append((start, position))
            start = position + 1
    while pending:
        low, high = pending.pop()
        if high - low + 1 <= cap or any(a <= low and high <= b for a, b in direct):
            continue
        weakest = min(range(low, high), key=lambda p: (weight[p], abs(2 * p + 1 - low - high), p))
        cut[weakest] = "chain"
        pending += [(low, weakest), (weakest + 1, high)]


def plan(
    members: Sequence[Member],
    versions: tuple[str, ...],
    evidence: dict[str, Evidence],
    orders: dict[str, dict[str, int]],
    cap: int,
    cuts: set[str] = frozenset(),  # type: ignore[assignment]
) -> list[tuple[tuple[str, ...], tuple[str, ...]]]:
    """Partition one segment stretch (members in address order) into modules with the signals that formed them.

    ORDERS maps each version to its function positions; CUTS names members that must start a module."""
    count = len(members)
    index = {member.name: position for position, member in enumerate(members)}
    joined: list[set[str]] = [set() for _ in range(max(count - 1, 0))]
    cut: list[str | None] = [None] * max(count - 1, 0)
    for position in range(count - 1):
        left, right = members[position], members[position + 1]
        # A version-only member stays with its predecessor (the first one with its successor).
        if set(right.versions) != set(versions) or (position == 0 and set(left.versions) != set(versions)):
            joined[position].add("version")
        holding = [v for v in versions if v in left.versions and v in right.versions]
        if right.name in cuts:
            cut[position] = "split"
        elif holding and all(
            (left.name, right.name) in evidence[v].cuts and orders[v][right.name] == orders[v][left.name] + 1
            for v in holding
        ):
            cut[position] = "padding"
    weight = [0] * max(count - 1, 0)
    direct: set[tuple[int, int]] = set()
    for version in versions:
        cover = [0] * max(count - 1, 0)
        for first, last, signal in evidence[version].joins:
            if first not in index or last not in index:
                continue
            low, high = sorted((index[first], index[last]))
            if any(cut[low:high]):
                continue
            direct.add((low, high))
            for position in range(low, high):
                joined[position].add(signal)
                cover[position] += 1
        weight = [max(pair) for pair in zip(weight, cover, strict=True)]
    _cut_chains(joined, cut, weight, direct, cap)
    # ahead[i]: members from i to the end of its strongly joined run, so a weak join counts the run it brings in.
    ahead = [1] * count
    for position in range(count - 2, -1, -1):
        if not cut[position] and joined[position] - _WEAK:
            ahead[position] = ahead[position + 1] + 1
    modules: list[tuple[tuple[str, ...], tuple[str, ...]]] = []
    current: list[str] = []
    signals: set[str] = set()
    atom: list[str] = []
    atom_signals: set[str] = set()

    def close(reason: str | None) -> None:
        nonlocal current, signals
        if current:
            modules.append((tuple(current), tuple(sorted(signals | ({reason} if reason else set())))))
        current, signals = [], set()

    def place(reason_after: str | None) -> None:
        nonlocal atom, atom_signals
        if current and len(current) + len(atom) > cap:
            close("cap")
        current.extend(atom)
        signals.update(atom_signals)
        atom, atom_signals = [], set()
        if reason_after:
            signals.add(reason_after)
            close(None)

    for position, member in enumerate(members):
        atom.append(member.name)
        if position == count - 1:
            place(None)
        elif cut[position]:
            place(cut[position])
        elif joined[position] - _WEAK or (joined[position] and len(current) + len(atom) + ahead[position + 1] <= cap):
            atom_signals.update(joined[position])
        else:
            place(None)
    close(None)
    return modules


def merged_order(
    members: Sequence[Member], versions: tuple[str, ...], orders: dict[str, dict[str, int]]
) -> list[Member]:
    """Members in one segment-major order every version agrees with: members all versions hold in address order, then
    each version's other members right after their predecessor in that version (segment start when there is none).
    Raw addresses differ between versions, so sorting version-only members by them scatters them among strangers."""
    placed: dict[str, list[Member]] = defaultdict(list)
    for member in sorted(members, key=lambda m: (m.segment, m.address, m.name)):
        if set(member.versions) == set(versions):
            placed[member.segment].append(member)
    for version in versions:
        held = sorted((m for m in members if version in m.versions), key=lambda m: orders[version][m.name])
        for segment in sorted({m.segment for m in held}):
            line = placed[segment]
            position = {m.name: index for index, m in enumerate(line)}
            insert = 0
            for member in (m for m in held if m.segment == segment):
                if member.name in position:
                    insert = position[member.name] + 1
                    continue
                line.insert(insert, member)
                insert += 1
                position = {m.name: index for index, m in enumerate(line)}
    return [member for segment in sorted(placed) for member in placed[segment]]


def facts(
    project: Project, version: str
) -> tuple[list[Function], Callable[[Function], list[int]], Callable[[int], bool], int | None]:
    """One version's functions, ROM word reader, resident constant test and _gp from its split files."""
    configured = project.version(version)
    image = configured.baserom.read_bytes()
    _, _, segments = split.layout(configured.split)
    segment_of: dict[str, str] = {}
    spans: list[tuple[int, int]] = []
    for segment in segments:
        name = split.plain(segment.fields.get("name", f"span_{int(segment.fields['start'], 0):X}"))
        for row in segment.rows:
            segment_of[row.path] = name
            if row.kind.lstrip(".") in ("rodata", "rdata"):
                address = split.address(row, configured.split)
                spans.append((address, address + split.end(row) - row.start))
    functions = [
        Function(Path(f.path).name, segment_of[f.path], f.start, f.end, f.address)
        for f in split.functions(project, version)
    ]
    _, symbols = split.symbols(configured.symbols)
    gp = symbols["_gp"][0] if "_gp" in symbols else None

    def words(function: Function) -> list[int]:
        data = image[function.start : function.end]
        return [int.from_bytes(data[i : i + 4], "big") for i in range(0, len(data) - 3, 4)]

    def constant(address: int) -> bool:
        return any(start <= address < end for start, end in spans)

    return functions, words, constant, gp


def infer(project: Project, value: Map, members: dict[str, Member]) -> Map:
    """Replace every `default` group (and every member no group holds) by inferred modules; keep the rest."""
    kept = {name: group for group in value.groups if group.evidence != "default" for name in group.members}
    cuts = {name for group in value.groups for name in group.split}
    evidence: dict[str, Evidence] = {}
    orders: dict[str, dict[str, int]] = {}
    for version in project.versions:
        functions, words, constant, gp = facts(project, version)
        evidence[version] = version_evidence(functions, words, constant, gp, value.cap)
        orders[version] = {f.name: i for i, f in enumerate(sorted(functions, key=lambda f: f.start))}
    groups: list[Group] = []
    stretch: list[Member] = []

    def flush() -> None:
        for names, signals in plan(stretch, project.versions, evidence, orders, value.cap, cuts):
            only = {n: members[n].versions for n in names if set(members[n].versions) != set(project.versions)}
            first = members[names[0]]
            split_cuts = tuple(n for n in names[1:] if n in cuts)
            groups.append(
                Group(f"code_{first.address:08X}", first.segment, "inferred", names, only, split_cuts, signals)
            )
        stretch.clear()

    for member in merged_order(list(members.values()), project.versions, orders):
        if stretch and stretch[-1].segment != member.segment:
            flush()
        group = kept.get(member.name)
        if group is None:
            stretch.append(member)
            continue
        flush()
        if group not in groups:
            groups.append(group)
    flush()
    return Map(value.cap, tuple(groups))
