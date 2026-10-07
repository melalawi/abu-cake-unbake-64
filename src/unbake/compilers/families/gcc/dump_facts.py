"""Observed GCC 2.x decisions and conservative RTL-to-word correspondence.

Only printed priorities are facts here. The allocator's separate formula hints
are deliberately not used. A ROM word has a hard register, never a known pseudo.
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Mapping
from typing import Any

from unbake.compilers.families.gcc.allocation import allocation
from unbake.config import Held

UNAVAILABLE = "unavailable"
_REG = r"\(reg(?:/[a-z]+)*:([A-Z]+) (\d+)(?: [^()]+)?\)"
_START = re.compile(
    r'\((?:insn|jump_insn|call_insn)(?:/[a-z]+)*(?::\w+)?\s+(\d+)\b|\(note\s+\d+[^\n]*?\("[^"\n]+"\)\s+(\d+)\)'
)


def records(text: str) -> list[dict[str, Any]]:
    """Scan RTL once; sequence wrappers do not emit words, their members do."""
    result = []
    line: int | str = UNAVAILABLE
    for match in _START.finditer(text):
        if match[2]:
            line = int(match[2])
            continue
        start = match.start()
        depth, quoted, escaped = 0, False, False
        end = start
        for end in range(start, len(text)):
            char = text[end]
            if quoted:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    quoted = False
            elif char == '"':
                quoted = True
            elif char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
                if not depth:
                    break
        if depth or quoted:
            continue
        rtl = text[start : end + 1]
        if "(sequence" in rtl:
            continue
        regs = [(mode, int(number)) for mode, number in re.findall(_REG, rtl)]
        dest = re.search(r"\(set\s+" + _REG, rtl)
        # REG_DEAD/EQUAL notes are evidence, not expression operands.
        expression = re.split(r"\)\s+-?\d+\s+\{", rtl, maxsplit=1)[0]
        expression_regs = [(mode, int(number)) for mode, number in re.findall(_REG, expression)]
        result.append(
            {
                "uid": int(match[1]),
                "rtl": rtl,
                "source_line": line,
                "registers": sorted({number for _, number in regs}),
                "destination": int(dest[2]) if dest else UNAVAILABLE,
                "mode": dest[1] if dest else UNAVAILABLE,
                "in_place": bool(dest and int(dest[2]) in [n for _, n in expression_regs[1:]]),
            }
        )
    return result


def _word(rtl: str) -> int | None:
    """Exact encodings for single-word integer/FP sets; expansions stay unmapped."""
    reg = _REG
    dest = re.search(r"\(set\s+" + reg + r"\s+", rtl)
    if dest is None:
        return None
    mode, rd = dest[1], int(dest[2])
    rhs = rtl[dest.end() :]
    if rd >= 64:
        return None
    binary = re.match(r"\((plus|minus|ashift|ashiftrt|lshiftrt):SI\s+" + reg + r"\s+\(const_int (-?\d+)\)\)", rhs)
    if binary and mode == "SI" and rd < 32:
        op, rm, rs_text, value_text = binary.groups()
        rs, value = int(rs_text), int(value_text)
        if rm != "SI" or rs >= 32:
            return None
        if op in ("plus", "minus"):
            value = value if op == "plus" else -value
            return (9 << 26) | (rs << 21) | (rd << 16) | (value & 65535) if -32768 <= value <= 32767 else None
        if 0 <= value <= 31:
            return (rs << 16) | (rd << 11) | (value << 6) | {"ashift": 0, "lshiftrt": 2, "ashiftrt": 3}[op]
    binary_regs = re.match(r"\((plus|minus):SI\s+" + reg + r"\s+" + reg + r"\)", rhs)
    if binary_regs and mode == "SI" and rd < 32:
        op, left_mode, rs_text, right_mode, rt_text = binary_regs.groups()
        if left_mode == right_mode == "SI" and int(rs_text) < 32 and int(rt_text) < 32:
            return (int(rs_text) << 21) | (int(rt_text) << 16) | (rd << 11) | (33 if op == "plus" else 35)
    fp = re.match(r"\(neg:(SF|DF)\s+" + reg + r"\)", rhs)
    if fp and mode == fp[1] == fp[2] and 32 <= rd < 64 and 32 <= int(fp[3]) < 64:
        return (17 << 26) | ((16 if mode == "SF" else 17) << 21) | ((int(fp[3]) - 32) << 11) | ((rd - 32) << 6) | 7
    load = re.match(r"\(mem(?:/[a-z]+)*:(SI|SF|DF)\s+(?:\(plus:SI\s+)?" + reg + r"(?:\s+\(const_int (-?\d+)\))?\)", rhs)
    if load and load[1] == mode and load[2] == "SI" and int(load[3]) < 32:
        immediate = int(load[4] or 0)
        op = {"SI": 35, "SF": 49, "DF": 53}[mode]
        rt = rd if mode == "SI" else rd - 32
        if 0 <= rt < 32 and -32768 <= immediate <= 32767:
            return (op << 26) | (int(load[3]) << 21) | (rt << 16) | (immediate & 65535)
    return None


def _printed_priorities(dumps: Mapping[str, str]) -> dict[int, int]:
    values: dict[int, int] = {}
    groups: dict[int, list[int]] = {}
    for line in dumps.get("galloc", "").splitlines():
        group = re.match(r";; allocno (\d+) pseudo (\d+(?:[ \t]+\d+)*) \S+ size", line)
        printed = re.match(r";; allocno (\d+) priority .* = (-?\d+)$", line)
        if group:
            groups[int(group[1])] = list(map(int, group[2].split()))
        if printed:
            values.update((n, int(printed[2])) for n in groups.get(int(printed[1]), []))
    for line in dumps.get("lalloc", "").splitlines():
        printed = re.match(r";; qty \d+ pseudo (\d+(?:[ \t]+\d+)*) .* priority (-?\d+)$", line)
        if printed:
            values.update((int(n), int(printed[2])) for n in printed[1].split())
    return values


def decisions(dumps: Mapping[str, str], candidate: tuple[int, ...], expanded: str) -> dict[str, Any]:
    """One parse per dump; word and UID indices are shared by every region."""
    limitations = []
    pseudos = []
    try:
        parsed = allocation(dumps)
    except Held as error:
        limitations.append(error.reason)
    else:
        printed_priorities = _printed_priorities(dumps)
        conflict_rows = {int(m[1]) for m in re.finditer(r"^;; (\d+) conflicts:", dumps.get("greg", ""), re.M)}
        for p in parsed.pseudos:
            pseudos.append(
                {
                    "pseudo": p.number,
                    "candidate_hard": list(range(p.hard, p.hard + p.words)) if p.hard is not None else UNAVAILABLE,
                    "references": p.references if p.references is not None else UNAVAILABLE,
                    "live_length": p.live_length if p.live_length is not None else UNAVAILABLE,
                    "rank": p.rank if p.rank is not None else UNAVAILABLE,
                    "priority": printed_priorities.get(p.number, UNAVAILABLE),
                    "hard_conflicts": [n for n in p.conflicts if n < 72] if p.number in conflict_rows else UNAVAILABLE,
                    "allocator": p.allocator,
                }
            )
    early = records(dumps.get("sched", "") or dumps.get("lreg", ""))
    final = records(dumps.get("dbr", ""))
    word_offsets: defaultdict[int, list[int]] = defaultdict(list)
    for i, word in enumerate(candidate):
        word_offsets[word].append(i * 4)
    encodings: defaultdict[int, list[dict[str, Any]]] = defaultdict(list)
    for insn in final:
        encoded = _word(insn["rtl"])
        if encoded is not None:
            encodings[encoded].append(insn)
    mapped = {}
    for word, insns in encodings.items():
        if len(insns) == len(word_offsets[word]) == 1:
            mapped[insns[0]["uid"]] = word_offsets[word]
    lines = expanded.splitlines()
    instructions = {}
    for insn in early:
        line = insn["source_line"]
        insn["source_text"] = (
            lines[line - 1].strip() if isinstance(line, int) and 0 < line <= len(lines) else UNAVAILABLE
        )
        insn["candidate_offsets"] = mapped.get(insn["uid"], [])
        instructions[insn["uid"]] = insn
    schedule = []
    for stage in ("sched", "sched2"):
        block: int | str = UNAVAILABLE
        priorities: dict[int, int] = {}
        for line in dumps.get(stage, "").splitlines():
            head = re.search(r"basic block number (\d+)", line)
            initial = re.search(r"insn\[\s*(\d+)\]: priority =\s*(-?\d+)", line)
            ready = re.search(r"ready list at T-(\d+):(.*), now(.*)", line)
            if head:
                block, priorities = int(head[1]), {}
            if initial:
                priorities[int(initial[1])] = int(initial[2])
            if ready:
                rows = []
                for uid_text, priority in re.findall(r"(\d+)\s+\(([0-9a-fA-F]+)\)", ready[2]):
                    uid, value = int(uid_text), int(priority, 16)
                    insn = instructions.get(uid, {})
                    base = priorities.get(uid)
                    promotion = (
                        "none"
                        if value == base
                        else "birth"
                        if base is not None
                        and 0 <= base < 0x1000000
                        and value == (base | 0x7F000000)
                        and not insn.get("in_place")
                        and insn.get("destination", UNAVAILABLE) != UNAVAILABLE
                        else UNAVAILABLE
                    )
                    rows.append(
                        {
                            "uid": uid,
                            "base_priority": priorities.get(uid, UNAVAILABLE),
                            "ready_priority": value,
                            "promotion": promotion,
                            "birth": True if promotion == "birth" else False if insn.get("in_place") else UNAVAILABLE,
                            "source_line": insn.get("source_line", UNAVAILABLE),
                            "source_text": insn.get("source_text", UNAVAILABLE),
                            "candidate_offsets": mapped.get(uid, []),
                            "mapping": "unique" if uid in mapped else "unavailable: missing or ambiguous RTL mapping",
                        }
                    )
                schedule.append(
                    {
                        "pass": stage,
                        "block": block,
                        "step": int(ready[1]),
                        "ready": rows,
                        "selected": list(map(int, re.findall(r"\d+", ready[3]))),
                    }
                )
    if not schedule:
        limitations.append("scheduler ready lists unavailable")
    return {
        "available": bool(pseudos or schedule),
        "family": "gcc",
        "pseudos": pseudos,
        "instructions": instructions,
        "schedule": schedule,
        "limitations": limitations,
    }
