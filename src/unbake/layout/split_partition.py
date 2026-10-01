"""Partition measured layouts into regions and function rows."""

from __future__ import annotations

import json
import re
from bisect import bisect_left, bisect_right
from collections.abc import Sequence
from itertools import pairwise
from pathlib import Path
from typing import TYPE_CHECKING

from unbake.layout import split
from unbake.project.config import Held

if TYPE_CHECKING:
    from unbake.project.config import Project
    from unbake.project.fingerprint import Region


def cut_segments(yaml: str, regions: Sequence[Region]) -> str:
    """Partition code on proven ROM boundaries, preserving intervening data rows."""
    if not regions:
        raise Held("split", "regions: required nonempty code regions")
    lines = yaml.splitlines(keepends=True)
    blocks: list[int] = []
    active = False
    for index, line in enumerate(lines):
        if line.strip() == "segments:":
            active = True
        elif active and re.match(r"  - ", line):
            blocks.append(index)
    if not blocks:
        raise Held("split", "segments: missing segments")
    spans = [
        (start, blocks[index + 1] if index + 1 < len(blocks) else len(lines)) for index, start in enumerate(blocks)
    ]
    code = []
    for start, end in spans:
        block = "".join(lines[start:end])
        if not re.search(r"^    type: code\s*$", block, re.M):
            continue
        fields = dict(re.findall(r"^    (\w+):\s*(\S+)\s*$", block, re.M))
        if "start" not in fields or "vram" not in fields:
            raise Held("split", "code segment start/vram: missing value")
        code.append((start, end, fields, block))
    if not code:
        raise Held("split", "segments: no code segments")
    biases = {int(fields["vram"], 0) - int(fields["start"], 0) for _, _, fields, _ in code}
    if len(biases) != 1:
        # Preserve independently loaded segments; compiler family assignment still
        # uses their measured functions, but a shared bias cannot partition them.
        return yaml
    if any(code[index][1] != code[index + 1][0] for index in range(len(code) - 1)):
        raise Held("split", "regions: noncontiguous code segments")
    bias = biases.pop()
    first, last = code[0][0], code[-1][1]
    rows: list[tuple[int, str]] = []
    for _, _, _, block in code:
        rows.extend(
            (int(match[1], 0), match[0])
            for match in re.finditer(r"^      - \[(0x[\da-fA-F]+|\d+),[^\n]+\]\s*$", block, re.M)
        )
    following = re.search(r"(?:start:\s*|- \[)(0x[\da-fA-F]+|\d+)", "".join(lines[last:]))
    if following is None:
        raise Held("split", "code segment end: missing value")
    limit = int(following[1], 0)
    boundaries = [int(code[0][2]["start"], 0)] + [region.start - bias for region in regions[1:]] + [limit]
    if boundaries != sorted(set(boundaries)):
        raise Held("split", "regions: overlapping or unordered boundaries")
    output = []
    for index, region in enumerate(regions):
        begin, end = boundaries[index : index + 2]
        selected = [(offset, row) for offset, row in rows if begin <= offset < end]
        if not selected or selected[0][0] != begin:
            preceding = [(offset, row) for offset, row in rows if offset < begin]
            measured = region.functions and region.functions[0].start == begin
            inherited = split.ROW.fullmatch(preceding[-1][1].rstrip() + "\n") if preceding else None
            if not measured or inherited is None or inherited["kind"] not in ("asm", "hasm"):
                raise Held("split", f"region {region.name}: no subsegment at ROM 0x{begin:X}")
            path = split.plain(inherited["path"]) + "_" + region.name
            selected.insert(0, (begin, f"      - [0x{begin:X}, asm, {json.dumps(path)}]"))
        output.extend(
            [
                f"  - name: {region.name}\n",
                "    type: code\n",
                f"    start: 0x{begin:X}\n",
                f"    vram: 0x{begin + bias:08X}\n",
                "    align: 4\n",
                "    subalign: 4\n",
            ]
        )
        if index == len(regions) - 1 and "bss_size" in code[-1][2]:
            output.append(f"    bss_size: {code[-1][2]['bss_size']}\n")
        if index == len(regions) - 1 and "bss_end" in code[-1][2]:
            output.append(f"    bss_end: {code[-1][2]['bss_end']}\n")
        output.append("    subsegments:\n")
        output.extend(row.rstrip() + "\n" for _, row in selected)
    following_text = "".join(lines[last:])
    named = re.search(r"^  - name:\s*(\S+)", code[-1][3], re.M)
    previous_name = named[1] if named else None
    if previous_name:
        following_text = re.sub(
            r"(^    follows_vram:\s*)" + re.escape(previous_name) + r"\s*$",
            lambda match: match[1] + regions[-1].name,
            following_text,
            flags=re.M,
        )
    return "".join(lines[:first] + output) + following_text


def cut_functions(yaml: str, functions: Sequence[split.Function]) -> str:
    """Replace merged text rows with measured per-function rows."""
    lines = yaml.splitlines(keepends=True)
    output = []
    offsets = sorted(
        {int(found[1], 0) for line in lines if (found := re.search(r"(?:- \[|start:\s*)(0x[\da-fA-F]+|\d+)", line))}
    )
    ordered = sorted(functions, key=lambda function: function.start)
    starts = [function.start for function in ordered]
    for line in lines:
        match = split.ROW.fullmatch(line)
        if not match or match["kind"] not in ("asm", "hasm"):
            output.append(line)
            continue
        start = int(match["start"], 0)
        following = bisect_right(offsets, start)
        if following == len(offsets):
            raise Held("split", "text row end: missing value")
        end = offsets[following]
        selected = ordered[bisect_left(starts, start) : bisect_left(starts, end)]
        if not selected:
            output.append(line)
            continue
        if selected[0].start != start:
            output.append(line)
        for function in selected:
            output.append(f"{match['indent']}- [0x{function.start:X}, asm, {function.name}]\n")
    return "".join(output)


def type_text(path: Path, measured: split.ExtractedText, *, text: str | None = None) -> str:
    """Split text rows at exact measured directive boundaries, retaining instructions."""
    _, lines, segments = split.parse_layout(path, split.read(path) if text is None else text)
    data: list[tuple[int, int]] = []
    for start, end in measured.data:
        if data and data[-1][1] == start:
            data[-1] = (data[-1][0], end)
        else:
            data.append((start, end))
    data_ends = [end for _, end in data]
    for segment in segments:
        for row in segment.rows:
            if row.kind not in ("asm", "hasm"):
                continue
            stop = split.end(row)
            runs = []
            for start, end in data[bisect_right(data_ends, row.start) :]:
                if start >= stop:
                    break
                runs.append((max(start, row.start), min(end, stop)))
            if not runs:
                continue
            boundaries = sorted({row.start, stop, *(offset for run in runs for offset in run)})
            output = []
            for start, end in pairwise(boundaries):
                kind = "data" if any(begin <= start and end <= finish for begin, finish in runs) else row.kind
                name = row.path if start == row.start else f"{row.path}_{kind}_{start:X}"
                output.append(split.replace_row(lines[row.line], row.match, start=f"0x{start:X}", kind=kind, path=name))
            lines[row.line] = "".join(output)
    return "".join(lines)


def classify(project: Project, version: str) -> list[split.Edit]:
    """Audit configured text rows and plan measured boundary/type repairs."""
    from unbake.layout.split_audit import audit

    return audit(project, version)[1]
