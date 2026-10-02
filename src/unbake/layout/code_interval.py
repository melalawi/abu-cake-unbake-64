"""Prove a ROM-backed data interval is referenced executable code before carving it."""

from __future__ import annotations

import hashlib
import re
import struct
import subprocess
import tempfile
from typing import Any

from unbake.layout import boundary, split
from unbake.layout.rodata_references import collect
from unbake.project.config import Held, Policy, Project, load_policy
from unbake.project.makefile import recipe


def prove(project: Project, version: str, start: int, end: int, policy: Policy | None = None) -> dict[str, Any]:
    configured = project.version(version)
    image = configured.baserom.read_bytes()
    if hashlib.sha1(image).hexdigest() != configured.baserom_sha1:
        raise Held("split", "split.code.rom_sha1: ROM differs from the confirmed cartridge")
    _, _, segments = split.layout(configured.split)
    selected = [r for s in segments for r in s.rows if r.start <= start < split.end(r)]
    if len(selected) != 1 or selected[0].kind not in ("data", "rodata", "rdata"):
        raise Held("split", "split.code.owner: select one ROM-backed data/rodata/rdata interval")
    row = selected[0]
    if start >= end or start % 4 or end % 4 or end > min(split.end(row), len(image)):
        raise Held("split", "split.code.interval: required increasing word-aligned offsets within one data row")
    address = split.address(row, configured.split) + start - row.start
    policy = policy or load_policy()
    with tempfile.NamedTemporaryFile(prefix="code-", suffix=".bin") as target:
        target.write(image[start:end])
        target.flush()
        decoded = subprocess.run(
            [
                str(policy.mips_objdump),
                "-D",
                "-z",
                "-b",
                "binary",
                "-m",
                "mips:4300",
                "-EB",
                f"--adjust-vma={address}",
                target.name,
            ],
            capture_output=True,
            text=True,
            check=False,
        )
    instructions = re.findall(r"^\s*[0-9a-fA-F]+:\s+([0-9a-fA-F]{8})\s+(\S+)", decoded.stdout, re.M)
    if decoded.returncode or len(instructions) != (end - start) // 4 or any(i[1].startswith(".") for i in instructions):
        raise Held("split", "split.code.decode: every word must decode as a VR4300 instruction")
    words = {at: struct.unpack_from(">I", image, at)[0] for at in range(start, end, 4)}
    functions = split.functions(project, version)
    sources: list[dict[str, Any]] = []
    known = {f.address for f in functions}
    referenced: set[int] = set()
    for function in functions:
        body = image[function.start : function.end]
        for index, (word,) in enumerate(struct.iter_unpack(">I", body)):
            pc = function.address + index * 4
            if word >> 26 in (2, 3) and ((pc + 4) & 0xF0000000) | ((word & 0x3FFFFFF) << 2) == address:
                sources.append({"kind": "direct-code-target", "function": function.name, "instruction": pc})
        refs, _ = collect(function.name, body, None, None)
        referenced.update(r.address for r in refs if r.type in ("address", "indexed"))
    # Callback/dispatch tables can store physical pointers. A bias is accepted
    # only when several neighbouring entries independently target declared code;
    # merely finding a numeric word equal to the requested entry is insufficient.
    mappings = recipe(project).resident_mappings.get(version, [])
    for segment in segments:
        for table in segment.rows:
            if table.kind not in ("data", "rodata", "rdata") or "vram" not in segment.fields:
                continue
            table_address = split.address(table, configured.split)
            stop = min(split.end(table), len(image))
            if not any(table_address <= ref < table_address + stop - table.start for ref in referenced):
                continue
            biases = {0, 0x80000000}
            biases.update(m["table_entry_bias"] for m in mappings if m["start"] <= table.start < m["end"])
            for at in range((table.start + 3) // 4 * 4, stop - 3, 4):
                pointer = struct.unpack_from(">I", image, at)[0]
                for bias in sorted(biases):
                    if (pointer + bias) & 0xFFFFFFFF != address:
                        continue
                    neighbours = {
                        (struct.unpack_from(">I", image, nearby)[0] + bias) & 0xFFFFFFFF
                        for nearby in range(max((table.start + 3) // 4 * 4, at - 128), min(stop - 3, at + 132), 4)
                    } & known
                    if len(neighbours) >= 2:
                        sources.append(
                            {
                                "kind": "referenced-code-pointer-table",
                                "table": table.path,
                                "rom_offset": at,
                                "pointer_bias": bias,
                                "known_targets": sorted(neighbours),
                            }
                        )
    if not sources:
        raise Held(
            "split",
            "split.code.reference: data-to-code correction needs a direct code target or referenced code-pointer table",
        )
    evidence = boundary.evidence(
        words,
        start,
        end,
        address - start,
        {s["kind"] for s in sources},
        {f.address - (address - start) for f in functions},
        4,
    )
    if not evidence.proven:
        raise Held("split", "split.code.control_flow: " + "; ".join(evidence.unproven))
    return {
        "version": version,
        "start": start,
        "end": end,
        "address": address,
        "sha256": hashlib.sha256(image[start:end]).hexdigest(),
        "decoder": "VR4300 objdump",
        "references": sources,
        "tags": evidence.tags,
    }
