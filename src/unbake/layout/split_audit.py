"""Audit extracted text against configured function rows."""

from __future__ import annotations

import os
import re
import struct
from bisect import bisect_left, bisect_right
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from unbake.layout import boundary, boundary_signatures, split

if TYPE_CHECKING:
    from unbake.project.config import Project


@dataclass(frozen=True)
class Finding:
    kind: str
    path: str
    start: int
    end: int
    name: str


WORD = re.compile(r"/\*\s*([\da-fA-F]+)\s+[\da-fA-F]{8}\s+[\da-fA-F]{8}\s*\*/\s*([^\n]+)")


def audit(
    project: Project, version: str, *, signatures: tuple[boundary_signatures.Signature, ...] | None = None
) -> tuple[list[Finding], list[split.Edit]]:
    """Plan exact cuts, data typing, and local jump fragment classification."""
    from unbake.layout.split_partition import type_text

    config = project.version(version)
    catalog = signatures
    if catalog is None:
        catalog = boundary_signatures.configured() if os.environ.get("UNBAKE_BOUNDARY_SIGNATURES") else ()
    image = config.baserom.read_bytes() if catalog else b""
    before, lines, segments = split.layout(config.split)
    _, symbols = split.symbols(config.symbols)
    ordered_symbols = sorted((entry[0], name) for name, entry in symbols.items())
    addresses = [address for address, _ in ordered_symbols]
    findings: list[Finding] = []
    paths: list[Path] = []
    for segment in segments:
        for row in segment.rows:
            if row.kind not in ("asm", "hasm"):
                continue
            path = project.asm / version / (row.path + ".s")
            paths.append(path)
            text = split.read(path)
            local_labels = set(re.findall(r"^\s*(?:alabel\s+([\w.$]+)|([.\w$]+):)", text, re.M))
            targets = {name for pair in local_labels for name in pair if name}
            emitted = [(int(match[1], 16), match[2].split("/*", 1)[0].strip()) for match in WORD.finditer(text)]
            stop = split.end(row)
            instructions = dict(emitted)
            vram = split.address(row, config.split)
            entries = {
                function.start: function.name
                for function in (
                    split.extracted_text(project, version, [path]).functions
                    if emitted and re.search(r"^(?:glabel|dlabel)\s+", text, re.M)
                    else []
                )
            }
            if catalog:
                for offset, signature in boundary_signatures.matches(image, row.start, stop, catalog).items():
                    entries[offset] = signature.name
            selected_symbols = ordered_symbols[
                bisect_right(addresses, vram) : bisect_left(addresses, vram + stop - row.start)
            ]
            for address, name in selected_symbols:
                offset = row.start + address - vram
                if row.start < offset < stop and offset in instructions and not instructions[offset].startswith("."):
                    entries.setdefault(offset, name)
            # A disassembler global jump label can still be part of this entry's
            # reachable body. Keep independent calls, SDK matches and frames;
            # do not turn every reachable jump destination into a function.
            words = {
                int(match[1], 16): int(match[2], 16)
                for match in re.finditer(r"/\*\s*([\da-fA-F]+)\s+[\da-fA-F]{8}\s+([\da-fA-F]{8})\s*\*/", text)
                if not instructions.get(int(match[1], 16), "").startswith(".")
            }
            reachable, _, _ = boundary.closure(words, row.start, stop, vram - row.start, set(entries))
            packed = b"".join(struct.pack(">I", words.get(at, 0)) for at in range(row.start, stop, 4))
            called = {
                row.start + offset
                for offset, sources in boundary.entries(packed, 0, len(packed), vram, ()).items()
                if "jal-target" in sources
            }
            sdk = set(boundary_signatures.matches(image, row.start, stop, catalog)) if catalog else set()
            entries = {
                at: name
                for at, name in entries.items()
                if at == row.start
                or at not in reachable
                or at in called
                or at in sdk
                or "compiler-stack-prologue" in boundary.shape(words, at, at + 4)
            }
            cuts = sorted(offset for offset in entries if row.start < offset < stop)
            boundaries = [row.start, *cuts, stop]
            rendered = []
            for index, start in enumerate(boundaries[:-1]):
                end = boundaries[index + 1]
                name = row.path if start == row.start else str(Path(row.path).with_name(entries[start]))
                kind = row.kind
                bodies = [body for offset, body in emitted if start <= offset < end]
                if start != row.start:
                    findings.append(Finding("hidden", row.path, start, end, entries[start]))
                if (
                    row.kind == "asm"
                    and len(bodies) >= 2
                    and re.fullmatch(r"j\s+[\w.$]+", bodies[0])
                    and (bodies[0].split()[1].startswith(".L") or bodies[0].split()[1] in targets)
                    and all(body == "nop" for body in bodies[1:])
                ):
                    kind = "hasm"
                    findings.append(Finding("fragment", row.path, start, end, Path(name).name))
                if any(body.startswith(".") for body in bodies):
                    findings.append(Finding("data", row.path, start, end, Path(name).name))
                template = lines[row.line]
                rendered.append(split.replace_row(template, row.match, start=f"0x{start:X}", kind=kind, path=name))
            # Leave unaffected rows byte-for-byte intact.
            if cuts or kind != row.kind:
                lines[row.line] = "".join(rendered)
    measured = split.extracted_text(project, version, paths) if paths else split.ExtractedText([], ())
    partitioned = "".join(lines)
    after = type_text(config.split, measured, text=partitioned)
    edits = [split.Edit(config.split, before, after, (version,))] if before != after else []
    return findings, edits
