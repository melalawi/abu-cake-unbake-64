"""Read GCC local/global allocator streams without guessing missing facts."""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from dataclasses import replace

from unbake.decomp.explain import Allocation, Pseudo
from unbake.project.config import Held


def dump_flags() -> tuple[str, ...]:
    return ("-da",)


def _stream(dumps: Mapping[str, str], name: str) -> str:
    if name not in dumps or not isinstance(dumps[name], str) or not dumps[name].strip():
        raise Held("explain", f"dumps.{name}: missing nonempty text")
    return dumps[name]


def allocation(dumps: Mapping[str, str]) -> Allocation:
    """Parse usage, final dispositions, conflicts and optional decision logs."""
    local, global_text = _stream(dumps, "lreg"), _stream(dumps, "greg")
    usage = {}
    for usage_match in re.finditer(r"Register (\d+) used (\d+) times across (\d+) insns", local):
        number, refs, live = map(int, usage_match.groups())
        if refs < 1 or live < 1:
            raise Held("explain", f"dumps.lreg.pseudo.{number}.references/live_length: expected positive values")
        usage[number] = (refs, live)
    if not usage:
        raise Held("explain", "dumps.lreg.usage: missing Register usage rows")
    dispositions = {}
    section = re.search(r"^;; Register dispositions:\s*\n([^;]*)", global_text, re.M)
    if section is None:
        raise Held("explain", "dumps.greg.dispositions: missing Register dispositions section")
    for disposition_match in re.finditer(r"(\d+) in (\d+)", section[1]):
        number, hard = map(int, disposition_match.groups())
        dispositions[number] = hard
    ranked = re.search(r"^;; (\d+) regs to allocate:([^\n]*)", global_text, re.M)
    if ranked is None:
        raise Held("explain", "dumps.greg.order: missing regs to allocate")
    order = list(map(int, ranked[2].split()))
    if len(order) != int(ranked[1]) or len(set(order)) != len(order):
        raise Held("explain", "dumps.greg.order: inconsistent pseudo count")
    conflicts = {
        int(m[1]): tuple(map(int, m[2].split()))
        for m in re.finditer(r"^;; (\d+) conflicts:([^\n]*)", global_text, re.M)
    }
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
            math.log2(refs) * refs / live,
            order.index(number) if number in order else None,
            conflicts.get(number, ()),
            "global" if number in order else "local",
            (),
            None,
            (),
        )
    block: int | None = None
    current: tuple[int, ...] | None = None
    quantities: dict[tuple[int | None, int], tuple[int, ...]] = {}
    for line in dumps.get("lalloc", "").splitlines():
        if match := re.match(r";; Block (\d+):", line):
            block, current = int(match[1]), None
        elif match := re.match(
            r";; qty (\d+) pseudo ([\d ]+) \S+ size \d+ refs (\d+) calls \d+ "
            r"class \S+ alternate \S+ life (\d+)-(\d+) priority (-?\d+)$",
            line,
        ):
            if block is None:
                raise Held("explain", "dumps.lalloc.block: missing value")
            quantity_text, numbers_text, refs_text, birth, death, priority = match.groups()
            numbers = tuple(map(int, numbers_text.split()))
            if int(death) <= int(birth) or int(refs_text) < 1:
                raise Held("explain", f"dumps.lalloc.qty.{quantity_text}.life/references: invalid values")
            quantities[block, int(quantity_text)] = numbers
            for number in numbers:
                if number not in facts:
                    raise Held("explain", f"dumps.lreg.pseudo.{number}.usage: missing value")
                facts[number] = replace(
                    facts[number],
                    references=int(refs_text),
                    live_range=(int(birth), int(death)),
                    live_length=int(death) - int(birth),
                    priority=float(priority),
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
        elif match := re.match(r";; pseudo (\d+) in (\d+) \(qty (\d+), offset (-?\d+)\)", line):
            number, hard, quantity, _offset = map(int, match.groups())
            if (block, quantity) not in quantities or number not in quantities[block, quantity]:
                raise Held("explain", f"dumps.lalloc.pseudo.{number}.quantity: missing membership")
            if number not in order and facts[number].hard != hard:
                raise Held("explain", f"dumps.lalloc.pseudo.{number}.hard: disagrees with final disposition")
    allocnos: dict[int, tuple[int, ...]] = {}
    current = None
    for line in dumps.get("galloc", "").splitlines():
        if match := re.match(
            r";; allocno (\d+) pseudo ([\d ]+) \S+ size \d+ refs (\d+) live_length (\d+) calls ", line
        ):
            allocno_text, numbers_text, refs_text, live_text = match.groups()
            allocnos[int(allocno_text)] = tuple(map(int, numbers_text.split()))
            for number in allocnos[int(allocno_text)]:
                if number not in facts or int(refs_text) < 1 or int(live_text) < 1:
                    raise Held("explain", f"dumps.galloc.pseudo.{number}.usage: missing or invalid value")
                facts[number] = replace(facts[number], references=int(refs_text), live_length=int(live_text))
        elif match := re.match(r";; allocno (\d+) pseudo (\d+) live range runs from insn (\d+) to insn (\d+)", line):
            _, number, birth, death = map(int, match.groups())
            if number not in facts or death < birth:
                raise Held("explain", f"dumps.galloc.pseudo.{number}.live_range: invalid value")
            facts[number] = replace(facts[number], live_range=(birth, death))
        elif match := re.match(r";; allocno (\d+) priority .* = (-?\d+)$", line):
            if int(match[1]) not in allocnos:
                raise Held("explain", f"dumps.galloc.allocno.{match[1]}: missing value")
            for number in allocnos[int(match[1])]:
                facts[number] = replace(facts[number], priority=float(match[2]))
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
    limitations = [
        "RTL ranges are allocator instruction positions, not machine word offsets; holders may be ambiguous."
    ]
    if "lalloc" not in dumps:
        limitations.append("Local decision log unavailable; usage priority is an estimate.")
    if "galloc" not in dumps:
        limitations.append("Global decision log unavailable; priority is log2(refs) * refs / live_length.")
    return Allocation(tuple(facts.values()), (), tuple(limitations), tuple(sorted(set(dispositions.values()))))
