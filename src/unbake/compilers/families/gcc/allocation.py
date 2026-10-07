"""Read GCC local/global allocator streams without guessing missing facts."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import replace

from unbake.config import Held
from unbake.decomp.explain import Allocation, Pseudo


def dump_flags() -> tuple[str, ...]:
    return ("-da",)


def global_priority(refs: int, live: int, words: int = 1) -> int:
    """GCC 2.x global allocno priority: floor_log2(refs) * refs / live * 10000 * words."""
    if refs < 1 or live < 1 or words < 1:
        raise Held("explain", "priority.references/live_length: expected positive values")
    return (refs.bit_length() - 1) * refs * 10000 * words // live


def flip(candidate: tuple[int, int], holder: tuple[int, int]) -> str:
    """Name the smallest single change that ranks the candidate ahead of the holder.

    Each pair is (references, live_length); global allocation visits higher priorities first.
    """
    priority = global_priority(*candidate)
    target = global_priority(*holder)
    live = min(candidate[1], (candidate[0].bit_length() - 1) * candidate[0] * 10000 // (target + 1))
    low, high = candidate[0], candidate[0]
    while global_priority(high, candidate[1]) <= target:
        high *= 2
    while low < high:
        middle = (low + high) // 2
        if global_priority(middle, candidate[1]) > target:
            high = middle
        else:
            low = middle + 1
    options = [f"candidate references >= {low}"]
    if priority:
        longer = max(holder[1], (holder[0].bit_length() - 1) * holder[0] * 10000 // priority + 1)
        options.append(f"holder live_length >= {longer}")
    if live:
        options.insert(0, f"candidate live_length <= {live}")
    return "; ".join(options)


def _stream(dumps: Mapping[str, str], name: str) -> str:
    if name not in dumps or not isinstance(dumps[name], str) or not dumps[name].strip():
        raise Held("explain", f"dumps.{name}: missing nonempty text")
    return dumps[name]


def _unknown(stream: str, line: str) -> Held:
    return Held("explain", f"dumps.{stream}.unknown_line: unsupported allocator row {line!r}")


def _row(pattern: str, line: str, stream: str) -> re.Match[str]:
    match = re.fullmatch(pattern, line)
    if match is None:
        raise _unknown(stream, line)
    return match


def _numbers(text: str, stream: str, line: str) -> tuple[int, ...]:
    if not re.fullmatch(r"(?:\d+(?:[ \t]+\d+)*)?", text.strip()):
        raise _unknown(stream, line)
    return tuple(map(int, text.split()))


def allocation(dumps: Mapping[str, str]) -> Allocation:
    """Parse usage, final dispositions, conflicts and optional decision logs.

    RTL and other diagnostic sections are not allocator rows. Recognized row
    prefixes must parse completely, so a new allocator format cannot masquerade
    as absent evidence or escape as a numeric conversion exception.
    """
    local, global_text = _stream(dumps, "lreg"), _stream(dumps, "greg")
    usage = {}
    match: re.Match[str] | None
    for line in local.splitlines():
        if line.startswith("("):
            break
        if not line.startswith("Register "):
            if not line.strip() or re.fullmatch(
                r";; Function \S+(?: \(.*\))?|\d+ (?:registers|basic blocks)\."
                r"|Basic block \d+: first insn \d+, last \d+\."
                r"|;; Register \d+ in \d+\."
                r"|Reached from blocks:(?:[ \t]+\d+)*(?:[ \t]+previous)?[ \t]*",
                line,
            ):
                continue
            if line.startswith("Registers live at start:"):
                _numbers(line.split(":", 1)[1], "lreg", line)
                continue
            raise _unknown("lreg", line)
        match = _row(r"Register (\d+) used (\d+) times across (-?\d+) insns(?: in block \d+)?(?:;.*|\.)?", line, "lreg")
        number, refs, live = map(int, match.groups())
        if refs < 1 or live < -2:
            raise Held("explain", f"dumps.lreg.pseudo.{number}.references/live_length: invalid values")
        usage[number] = (refs, live)
    if not re.search(r"^;; Register dispositions:", global_text, re.M):
        raise Held("explain", "dumps.greg.dispositions: missing Register dispositions section")
    dispositions = {}
    order: list[int] = []
    words: dict[int, int] = {}
    ranks: dict[int, int] = {}
    groups: list[tuple[int, ...]] = []
    conflicts = {}
    in_dispositions = False
    have_dispositions = False
    have_order = False
    in_rtl = False
    for line in global_text.splitlines():
        if line.startswith("("):
            in_rtl = True
        if in_rtl:
            continue
        if in_dispositions and line.startswith(";;"):
            in_dispositions = False
        if in_dispositions:
            _row(r"(?:\d+ in \d+\s*)*", line.strip(), "greg")
            for match in re.finditer(r"(\d+) in (\d+)", line):
                number, hard = map(int, match.groups())
                dispositions[number] = hard
        elif line.startswith(";; Register dispositions"):
            _row(r";; Register dispositions:", line, "greg")
            in_dispositions = have_dispositions = True
        elif re.match(r";; .*regs to allocate", line):
            ranked = _row(r";; (\d+) regs to allocate:([ \t\d()+]*)", line, "greg")
            # GCC prints the allocno's size in hard-register words after its
            # pseudo number, e.g. 73 (2). It is not a second pseudo or refs.
            remainder = ranked[2].strip()
            while remainder:
                match = re.match(r"(\d+(?:\+\d+)*)(?:[ \t]+\((\d+)\))?(?:[ \t]+|$)", remainder)
                if match is None:
                    raise _unknown("greg", line)
                numbers = tuple(map(int, match[1].split("+")))
                size = int(match[2]) if match[2] else 1
                if size < 1:
                    raise Held("explain", f"dumps.greg.pseudo.{numbers[0]}.words: expected positive value")
                for number in numbers:
                    words[number] = size
                    ranks[number] = len(groups)
                    order.append(number)
                groups.append(numbers)
                remainder = remainder[match.end() :]
            if have_order or len(groups) != int(ranked[1]) or len(set(order)) != len(order):
                raise Held("explain", "dumps.greg.order: inconsistent pseudo count")
            have_order = True
        elif re.match(r";; .*conflicts:", line):
            match = _row(r";; (\d+) conflicts:(.*)", line, "greg")
            conflicts[int(match[1])] = _numbers(match[2], "greg", line)
        elif re.match(r";; .*preferences:", line):
            match = _row(r";; (\d+) preferences:(.*)", line, "greg")
            _numbers(match[2], "greg", line)
        elif re.fullmatch(
            r";; Need \d+ (?:regs?|nongroup regs?|groups? \(\w+mode\)) "
            r"of class \w+ \(for insn \d+\)\.|Spilling reg \d+\."
            r"| Register \d+ now (?:on stack|in \d+)\.",
            line,
        ):
            # SN64 reload diagnostics precede final dispositions.
            continue
        elif line.startswith(";; Hard regs used:"):
            _numbers(line.split(":", 1)[1], "greg", line)
        elif line.strip() and not re.fullmatch(r";;(?:\s*| Function \S+(?: \(.*\))?)", line):
            raise _unknown("greg", line)
    if not have_dispositions:
        raise Held("explain", "dumps.greg.dispositions: missing Register dispositions section")
    if not have_order and (
        not {number for number, (_, live) in usage.items() if live > 0} <= dispositions.keys()
        or conflicts
        or re.search(r"^;; allocno \d+", dumps.get("galloc", ""), re.M)
    ):
        raise Held("explain", "dumps.greg.order: missing regs to allocate")
    facts = {}
    for number in sorted(usage.keys() | dispositions.keys() | set(order)):
        if number not in usage:
            raise Held("explain", f"dumps.lreg.pseudo.{number}.usage: missing value")
        refs, live = usage[number]
        facts[number] = Pseudo(
            number,
            dispositions.get(number),
            refs,
            live,
            None,
            global_priority(refs, live, words.get(number, 1)) if live > 0 else None,
            ranks.get(number),
            conflicts.get(number, ()),
            "global" if number in order else "local" if number in dispositions else "unallocated",
            (),
            None,
            (),
            words.get(number, 1),
        )
    for group in groups:
        refs = sum(usage[number][0] for number in group)
        live = max(usage[number][1] for number in group)
        priority = global_priority(refs, live, words[group[0]]) if live > 0 else None
        for number in group:
            facts[number] = replace(facts[number], priority=priority)
    block: int | None = None
    current: tuple[int, ...] | None = None
    quantities: dict[tuple[int | None, int], tuple[int, ...]] = {}
    for line in dumps.get("lalloc", "").splitlines():
        if match := re.match(r";; Block (\d+):$", line):
            block, current = int(match[1]), None
        elif match := re.match(
            r";; qty (\d+) pseudo (\d+(?:[ \t]+\d+)*) \S+ size (\d+) refs (\d+) calls \d+ "
            r"class \S+ alternate \S+ life (\d+)-(\d+) priority (-?\d+)$",
            line,
        ):
            if block is None:
                raise Held("explain", "dumps.lalloc.block: missing value")
            quantity_text, numbers_text, size_text, refs_text, birth, death, priority_text = match.groups()
            numbers = _numbers(numbers_text, "lalloc", line)
            if int(death) <= int(birth) or int(refs_text) < 1 or int(size_text) < 1:
                raise Held("explain", f"dumps.lalloc.qty.{quantity_text}.life/references: invalid values")
            quantities[block, int(quantity_text)] = numbers
            for number in numbers:
                if number not in facts:
                    raise Held("explain", f"dumps.lreg.pseudo.{number}.usage: missing value")
                facts[number] = replace(
                    facts[number],
                    words=int(size_text),
                    references=int(refs_text),
                    live_range=(int(birth), int(death)),
                    live_length=int(death) - int(birth),
                    priority=int(priority_text),
                )
        elif match := re.match(r";; qty (\d+) wants ", line):
            current = quantities.get((block, int(match[1])))
            if current is None:
                raise Held("explain", f"dumps.lalloc.qty.{match[1]}: missing quantity")
        elif match := re.match(r";;\s+(\d+) rejected: (.*)", line):
            if current is None:
                raise Held("explain", "dumps.lalloc.request: missing quantity request")
            for number in current:
                p = facts[number]
                facts[number] = replace(p, rejections=(*p.rejections, (int(match[1]), match[2])))
        elif match := re.match(r";; pseudo (\d+) in (\d+) \(qty (\d+), offset (-?\d+)\)$", line):
            number, hard, quantity, _offset = map(int, match.groups())
            if (block, quantity) not in quantities or number not in quantities[block, quantity]:
                raise Held("explain", f"dumps.lalloc.pseudo.{number}.quantity: missing membership")
            if number not in order and facts[number].hard != hard:
                raise Held("explain", f"dumps.lalloc.pseudo.{number}.hard: disagrees with final disposition")
        elif line.strip() and not re.fullmatch(r";; Function \S+", line):
            raise _unknown("lalloc", line)
    allocnos: dict[int, tuple[int, ...]] = {}
    current = None
    for line in dumps.get("galloc", "").splitlines():
        if match := re.match(
            r";; allocno (\d+) pseudo (\d+(?:[ \t]+\d+)*) \S+ size (\d+) refs (\d+) live_length (-?\d+) calls \d+$",
            line,
        ):
            allocno_text, numbers_text, size_text, refs_text, live_text = match.groups()
            allocnos[int(allocno_text)] = _numbers(numbers_text, "galloc", line)
            for number in allocnos[int(allocno_text)]:
                if number not in facts or int(refs_text) < 1 or int(live_text) < -2 or int(size_text) < 1:
                    raise Held("explain", f"dumps.galloc.pseudo.{number}.usage: missing or invalid value")
                facts[number] = replace(
                    facts[number],
                    references=int(refs_text),
                    live_length=int(live_text),
                    words=int(size_text),
                    priority=global_priority(int(refs_text), int(live_text), int(size_text))
                    if int(live_text) > 0
                    else None,
                )
        elif match := re.match(r";; allocno (\d+) pseudo (\d+) live range runs from insn (\d+) to insn (\d+)$", line):
            _, number, birth, death = map(int, match.groups())
            if number not in facts or death < birth:
                raise Held("explain", f"dumps.galloc.pseudo.{number}.live_range: invalid value")
            facts[number] = replace(facts[number], live_range=(birth, death))
        elif match := re.match(r";; allocno (\d+) priority .* = (-?\d+)$", line):
            if int(match[1]) not in allocnos:
                raise Held("explain", f"dumps.galloc.allocno.{match[1]}: missing value")
            for number in allocnos[int(match[1])]:
                facts[number] = replace(facts[number], priority=int(match[2]))
        elif match := re.match(r";; allocno (\d+).*seeking ", line):
            current = allocnos.get(int(match[1]))
            if current is None:
                raise Held("explain", f"dumps.galloc.allocno.{match[1]}: missing value")
        elif match := re.match(r";;\s+(\d+) rejected: (.*)", line):
            if current is None:
                raise Held("explain", "dumps.galloc.request: missing allocation request")
            for number in current:
                p = facts[number]
                facts[number] = replace(p, rejections=(*p.rejections, (int(match[1]), match[2])))
        elif line.strip() and not re.fullmatch(r";; Function \S+", line):
            raise _unknown("galloc", line)
    limitations = [
        "RTL ranges are allocator instruction positions, not machine word offsets; holders may be ambiguous."
    ]
    if "lalloc" not in dumps:
        limitations.append("Local decision log unavailable; usage priority is an estimate.")
    if "galloc" not in dumps:
        limitations.append(
            "Global decision log unavailable; priority is floor_log2(refs) * refs / live_length * 10000 * words."
        )
    return Allocation(tuple(facts.values()), (), tuple(limitations), tuple(sorted(set(dispositions.values()))))
