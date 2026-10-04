"""Report conservative boundary evidence from the existing layout and audit.

Call report(project, signatures=path) with an offline JSON SDK catalog (see
boundary_signatures.load). With signatures=None, every row explicitly records
missing SDK identification and remains unproven. No network access occurs.
"""

from __future__ import annotations

import hashlib
import json
import struct
from dataclasses import asdict, replace
from pathlib import Path
from typing import TYPE_CHECKING

from unbake.layout import boundary, boundary_signatures, split
from unbake.layout.boundary import Boundary
from unbake.layout.split_audit import audit
from unbake.project.config import Held
from unbake.project_tools import atomic as atomic_files

if TYPE_CHECKING:
    from unbake.project.config import Project


def report(project: Project, *, signatures: Path | None) -> dict[str, list[Boundary]]:
    """Audit once, then tag every proposed function without publishing edits."""
    catalog = boundary_signatures.load(signatures) if signatures is not None else ()
    result: dict[str, list[Boundary]] = {}
    bodies: dict[bytes, list[tuple[str, int]]] = {}
    for version in project.versions:
        config = project.version(version)
        image = config.baserom.read_bytes()
        _, edits = audit(project, version, signatures=catalog)
        text = edits[0].after if edits else split.read(config.split)
        _, _, segments = split.parse_layout(config.split, text)
        _, _, configured_segments = split.parse_layout(config.split, split.read(config.split))
        paths = [
            project.asm / version / (row.path + ".s")
            for segment in configured_segments
            for row in segment.rows
            if row.kind in ("asm", "hasm")
        ]
        measured = split.extracted_text(project, version, paths) if paths else split.ExtractedText([], ())
        data_offsets = {at for begin, end in measured.data for at in range(begin, end, 4)}
        rows = [row for segment in segments for row in segment.rows if row.kind in ("asm", "c", "hasm")]
        seeds: dict[int, set[str]] = {}
        mappings: dict[int, int] = {}
        for row in rows:
            mappings[row.start] = split.address(row, config.split) - row.start
        for segment in segments:
            text_rows = [row for row in segment.rows if row.kind in ("asm", "c", "hasm")]
            if not text_rows:
                continue
            begin = text_rows[0].start
            stop = split.end(text_rows[-1])
            bias = mappings[begin]
            # Discovery uses the measured loaded span, so calls across rows seed entries.
            for start, sources in boundary.entries(image, begin, stop, bias, catalog).items():
                sources.discard("loaded-entry")
                seeds.setdefault(start, set()).update(sources)
        for row in rows:
            bias = mappings[row.start]
            segment_start = split.number(row.segment.fields.get("start"), "segment.start")
            if row.start == segment_start and row.start == 0x1000:
                seeds.setdefault(row.start, set()).add("boot-entry")
            if row.start + bias in (0x80000000, 0x80000080, 0x80000180):
                seeds.setdefault(row.start, set()).add("vector-entry")
        # Only typed data supplies pointer evidence, never arbitrary instruction words.
        for segment in segments:
            for row in segment.rows:
                if row.kind not in ("data", "rodata", ".data", ".rodata"):
                    continue
                for at in range(row.start, min(split.end(row), len(image)) - 3, 4):
                    address = struct.unpack_from(">I", image, at)[0]
                    for target in rows:
                        if address == target.start + mappings[target.start]:
                            seeds.setdefault(target.start, set()).add(f"{row.kind}-pointer:0x{at:X}")
        proofs = []
        known = {row.start for row in rows}
        for row in rows:
            stop = split.end(row)
            if row.start < 0 or stop > len(image) or row.start % 4 or stop % 4:
                raise Held("boundary", f"VERSION {version} {row.path}: invalid ROM word range")
            words = {
                at: struct.unpack_from(">I", image, at)[0] for at in range(row.start, stop, 4) if at not in data_offsets
            }
            proof = boundary.evidence(
                words,
                row.start,
                stop,
                mappings[row.start],
                seeds.get(row.start, set()),
                known,
                split.number(row.segment.fields.get("subalign", "4"), "subalign"),
            )
            if not catalog:
                proof = replace(proof, unproven=(*proof.unproven, "UNBAKE_BOUNDARY_SIGNATURES: SDK catalog absent"))
            proofs.append(proof)
            digest = hashlib.sha256(image[row.start : stop]).digest()
            bodies.setdefault(digest, []).append((version, len(proofs) - 1))
        result[version] = proofs
    for twins in bodies.values():
        versions = {version for version, _ in twins}
        if len(versions) < 2:
            continue
        for version, index in twins:
            proof = result[version][index]
            result[version][index] = replace(
                proof, tags=(*proof.tags, "identical-bytes-boundary-agreement:" + ",".join(sorted(versions)))
            )
    return result


def write(project: Project, destination: Path, *, signatures: Path | None) -> None:
    proofs = report(project, signatures=signatures)
    document = {
        version: {
            "proven_count": sum(proof.proven for proof in rows),
            "unproven": [asdict(proof) for proof in rows if not proof.proven],
            "boundaries": [asdict(proof) for proof in rows],
        }
        for version, rows in proofs.items()
    }
    if destination.is_symlink():
        raise Held("boundary", f"{destination.name}: refusing symlink output")
    atomic_files.text(destination, json.dumps(document, indent=2) + "\n")
