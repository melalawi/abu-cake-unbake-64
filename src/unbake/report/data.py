"""Source data coverage from dependency-bound final-link evidence in the existing Ledger."""

from __future__ import annotations

import hashlib
import json
import re
import struct
from dataclasses import dataclass
from itertools import pairwise
from pathlib import Path
from typing import TYPE_CHECKING, Any, NoReturn

from unbake import cache, inputs
from unbake.config import Held, Host, Project
from unbake.layout import split
from unbake.process import named as cause_named
from unbake.work import attempts

if TYPE_CHECKING:
    from unbake.objects.rodata import InitializedSection


@dataclass(frozen=True)
class Interval:
    start: int
    end: int
    kind: str
    path: str
    address: int | None


@dataclass(frozen=True)
class Coverage:
    intervals: tuple[Interval, ...]
    matched: tuple[tuple[int, int], ...]
    manifest: dict[str, Any]

    def bytes_in(self, start: int, end: int) -> int:
        return sum(max(0, min(end, stop) - max(start, begin)) for begin, stop in self.matched)


def refuse(detail: str) -> NoReturn:
    raise Held(cause_named("data.evidence", f"data.evidence: {detail}", owner="report.data", stage="report"))


def digest(value: Any) -> str:
    return hashlib.sha256(attempts.encoded(value)).hexdigest()


def integer(value: Any, name: str, *, positive: bool = False) -> int:
    if type(value) is not int or value < int(positive):
        refuse(f"{name}: nonnegative integer required")
    return int(value)


def sha(value: Any, name: str) -> str:
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        refuse(f"{name}: sha256 required")
    return str(value)


def declared(project: Project, version: str) -> tuple[tuple[Interval, ...], int | None]:
    text, _, segments = split.layout(project.version(version).split)
    rows = []
    for segment in segments:
        for index, row in enumerate(segment.rows):
            if row.kind in split.CODE_KINDS or row.kind.lstrip(".") in {"bss", "sbss"}:
                continue
            end = segment.rows[index + 1].start if index + 1 < len(segment.rows) else segment.end
            if end is None or end <= row.start:
                refuse(f"VERSION {version} {row.path}: missing or invalid declared extent")
            address = None
            if "vram" in segment.fields:
                address = (
                    split.number(segment.fields["vram"], "segment vram")
                    + row.start
                    - split.number(segment.fields["start"], "segment start")
                )
            rows.append(Interval(row.start, end, row.kind, row.path, address))
    rows.sort(key=lambda row: row.start)
    if any(left.end > right.start for left, right in pairwise(rows)):
        refuse(f"VERSION {version}: overlapping declared data")
    endings = re.findall(r"^\s*-\s*\[(0[xX][0-9a-fA-F]+|[0-9]+)\]\s*$", text, re.M)
    rom_size = max((int(value, 0) if value.lower().startswith("0x") else int(value) for value in endings), default=None)
    return tuple(rows), rom_size


def snapshots(project: Project) -> dict[str, dict[str, Any] | None]:
    history = attempts.ledger(project)
    return {version: history.latest("native.data", version) for version in project.versions}


def identity(records: dict[str, dict[str, Any] | None]) -> str:
    return digest(records)


def required(project: Project, version: str) -> set[str]:
    meta = project.version(version)
    return {
        "config.toml",
        "layout.toml",
        "tools/compilers.sha256",
        "tools/n64link.version",
        "Makefile",
        "units.mk",
        meta.split.relative_to(project.root).as_posix(),
        meta.symbols.relative_to(project.root).as_posix(),
        f"versions/{version}/{project.name}.ld",
        f"versions/{version}/symbols.ld",
    }


def capture_inputs(
    project: Project, source: Path, version: str, command: list[str], *, compiler: str, non_matching: bool
) -> inputs.DependencySet:
    """Capture retained-source and native recipe inputs before preprocessing, never from a later source."""
    from unbake.project.headers import Graph

    graph = Graph.capture(project)
    closure = graph.closure((source,), command)
    pins = {pin.path: pin for pin in closure.dependency_set.files}
    paths = {project.root / name for name in required(project, version)} | {project.version(version).baserom}
    for path in sorted(paths):
        pin = inputs.file_pin(path, root=project.root, root_id="project", reuse=cache.configured())
        pins[pin.path] = pin
    return inputs.DependencySet(
        tuple(pins[path] for path in sorted(pins)),
        {
            **closure.dependency_set.values,
            "version": version,
            "compiler": compiler,
            "non_matching": non_matching,
            "dependencies_unknown": closure.unknown,
        },
        {**closure.dependency_set.recipes, "data.emission": inputs.digest(Path(__file__), algorithm="sha256")},
    )


def assert_inputs(project: Project, dependencies: inputs.DependencySet) -> None:
    """Include missing lookup probes: a newly appearing earlier header changes the native input too."""
    for pin in dependencies.files:
        if pin.path.root != "project":
            continue
        current = inputs.file_pin(
            project.root.joinpath(*pin.path.parts), root=project.root, root_id="project", reuse=False
        )
        if current != pin:
            raise Held(
                cause_named(
                    "native.input_changed",
                    f"{pin.path.name}: changed during native production",
                    owner="runner",
                    stage="compile",
                )
            )


def record_linked(
    project: Project,
    host: Host,
    source: Path,
    version: str,
    original: Path,
    final: Path,
    layout: tuple[InitializedSection, ...],
) -> str | None:
    """Record only named retained definitions emitted by this compilation's final relocated ELF."""
    from unbake import runner
    from unbake.objects.elf import Object
    from unbake.project.headers import Graph

    captured = original.with_suffix(".inputs.json")
    if not layout or not source.is_relative_to(project.src) or not captured.is_file():
        return None
    document = json.loads(captured.read_text())
    dependencies = attempts.dependency_record(document)
    if (
        dependencies.values.get("dependencies_unknown")
        or dependencies.values.get("non_matching")
        or dependencies.values.get("object_sha256") != inputs.digest(original, algorithm="sha256", reuse=False)
        or dependencies.values.get("compiler") != runner._compiler_pins(project, source.stem)
        or dependencies.values.get("link_tools") != link_tools(host)
        or current_dependencies(project, {"dependencies": document}, version) is not None
        or inputs.digest(project.version(version).baserom, algorithm="sha1", reuse=cache.configured())
        != project.version(version).baserom_sha1
    ):
        return None
    assert_inputs(project, dependencies)
    definitions = Graph.capture(project).initialized_definitions(project, source, version)
    if not definitions:
        return None
    source_hash = inputs.digest(source, algorithm="sha256", reuse=False)
    owner = inputs.LogicalPath("project", source.relative_to(project.root).parts)
    obj = Object(final)
    if struct.unpack_from(">H", obj.data, 16)[0] != 2:
        refuse("final relocated executable ELF required")
    sections, extents = [], []
    intervals, _ = declared(project, version)
    with project.version(version).baserom.open("rb") as rom:
        for item in layout:
            index = obj.section(item.output_name)
            if index is None or obj.sections[index][3] != item.address or obj.sections[index][5] != item.size:
                refuse("final initialized section missing or differs from native placement")
            emitted = obj.content(index)
            if obj.sections[index][1] != 1 or not obj.sections[index][2] & 2 or obj.relocations(index):
                refuse("final initialized section is not allocated, relocated PROGBITS")
            sections.append(
                {
                    "name": item.output_name,
                    "address": item.address,
                    "size": item.size,
                    "sha256": hashlib.sha256(emitted).hexdigest(),
                }
            )
            for name, offset, size in item.symbols:
                if name not in definitions:
                    continue
                start = item.rom_offset + offset
                address = item.address + offset
                if not any(
                    row.start <= start < start + size <= row.end
                    and row.address is not None
                    and address == row.address + start - row.start
                    for row in intervals
                ):
                    continue
                rom.seek(start)
                resident = rom.read(size)
                linked = emitted[offset : offset + size]
                if len(resident) != size or resident != linked:
                    continue
                extents.append(
                    {
                        "version": version,
                        "owner_source": {"root": owner.root, "parts": list(owner.parts)},
                        "symbol": name,
                        "section": item.output_name,
                        "address": address,
                        "rom_offset": start,
                        "size": size,
                        "source_sha256": source_hash,
                        "bytes_sha256": hashlib.sha256(linked).hexdigest(),
                        "rom_bytes_sha256": hashlib.sha256(resident).hexdigest(),
                        "definition_proof_id": definitions[name],
                    }
                )
    if not extents:
        return None
    evidence = {
        "final_artifact_sha256": inputs.digest(final, algorithm="sha256", reuse=False),
        "input_identity": dependencies.digest,
        "sections": sections,
        "extents": extents,
        "unavailable_reason": None,
    }
    history = attempts.ledger(project)
    prior = history.latest("native.data", version)
    retained = []
    pins = {pin.path: pin for pin in dependencies.files}
    if prior is not None and current_dependencies(project, prior, version) is None:
        previous = prior["result"]["value"].get("native_data", {})
        if (
            digest(previous) in prior["result"]["proof_ids"]
            and previous.get("rom_sha1") == project.version(version).baserom_sha1
        ):
            retained = [
                row
                for row in previous.get("evidence", [])
                if all(
                    extent["owner_source"] != {"root": owner.root, "parts": list(owner.parts)}
                    for extent in row["extents"]
                )
            ]
            if retained:
                for pin in attempts.dependency_record(prior["dependencies"]).files:
                    pins.setdefault(pin.path, pin)
    dependencies = inputs.DependencySet(
        tuple(pins[path] for path in sorted(pins)), dependencies.values, dependencies.recipes
    )
    payload = {
        "schema": 1,
        "version": version,
        "rom_sha1": project.version(version).baserom_sha1,
        "rom_size": project.version(version).baserom.stat().st_size,
        "evidence": [*retained, evidence],
    }
    assert_inputs(project, dependencies)
    operation = attempts.Operation.make(project, "native.data", version, {}, dependencies)
    return history.record(
        operation, attempts.Outcome(operation.id, "ok", {"native_data": payload}, None, {}, (digest(payload),))
    )


def link_tools(host: Host) -> dict[str, str]:
    return {
        name: inputs.digest(Path(getattr(host, name)), algorithm="sha256", reuse=cache.configured())
        for name in ("mips_ld", "mips_objcopy", "n64link")
    }


def current_dependencies(project: Project, event: dict[str, Any], version: str) -> str | None:
    dependencies = attempts.dependency_record(event["dependencies"])
    if dependencies.values.get("dependencies_unknown") or not dependencies.recipes:
        return "data.dependencies.unknown"
    pinned = set()
    for pin in dependencies.files:
        if pin.path.root != "project":
            return "data.dependencies.external"
        path = project.root.joinpath(*pin.path.parts)
        pinned.add(path.relative_to(project.root).as_posix())
        if pin.state == "missing" and not path.exists() and not path.is_symlink():
            if path.relative_to(project.root).as_posix() in required(project, version):
                return "data.dependencies.missing"
            continue
        if pin.state != "file":
            return "data.dependencies.missing"
        if path == project.version(version).baserom and not path.is_file():
            # Public CI binds the native ROM identity through committed config,
            # rather than acquiring private ROM bytes or invoking native work.
            continue
        if not path.is_file():
            return "data.dependencies.missing"
        if inputs.digest(path, algorithm="sha256", reuse=cache.configured()) != pin.sha256:
            return "data.dependencies.changed"
    if not required(project, version) <= pinned:
        return "data.dependencies.incomplete"
    return None


def coverage(
    project: Project, version: str, sources: dict[str, tuple[str, set[str], bool]], record: dict[str, Any] | None
) -> Coverage:
    intervals, rom_size = declared(project, version)
    total = sum(row.end - row.start for row in intervals)
    manifest: dict[str, Any] = {
        "state": "unmeasured" if total else "not-declared",
        "declared_bytes": total,
        "verified_bytes": 0,
        "unmeasured_bytes": total,
        "rom_bytes": rom_size,
        "evidence_identity": digest(record),
        "proof_event": None,
        "invalidations": 0,
        "extents": [],
        "causes": [],
    }

    def unknown(reason: str, *, invalidated: bool = False) -> Coverage:
        manifest["causes"].append({"key": reason, "unmeasured_bytes": total})
        manifest["invalidations"] = int(invalidated)
        return Coverage(intervals, (), manifest)

    if record is None:
        return unknown("data.proof.missing") if total else Coverage(intervals, (), manifest)
    result = record["result"]
    payload = result["value"].get("native_data")
    if not isinstance(payload, dict) or set(payload) != {"schema", "version", "rom_sha1", "rom_size", "evidence"}:
        refuse("complete native data snapshot required")
    if (
        type(payload["schema"]) is not int
        or payload["schema"] != 1
        or payload["version"] != version
        or not isinstance(payload["evidence"], list)
    ):
        refuse("invalid native data snapshot version/schema")
    size = integer(payload["rom_size"], "rom_size", positive=True)
    if not isinstance(payload["rom_sha1"], str) or re.fullmatch(r"[0-9a-f]{40}", payload["rom_sha1"]) is None:
        refuse("ROM sha1 required")
    for evidence in payload["evidence"]:
        if not isinstance(evidence, dict) or not isinstance(evidence.get("extents"), list):
            refuse("native data evidence and extents array required")
        for extent in evidence["extents"]:
            owner = extent.get("owner_source") if isinstance(extent, dict) else None
            if (
                not isinstance(owner, dict)
                or set(owner) != {"root", "parts"}
                or owner["root"] != "project"
                or not isinstance(owner["parts"], list)
                or any(not isinstance(part, str) for part in owner["parts"])
            ):
                refuse("portable project source identifier required")
            inputs.LogicalPath(owner["root"], tuple(owner["parts"]))
    if result["state"] not in {"ok", "committed"} or digest(payload) not in result["proof_ids"]:
        return unknown("data.proof.unavailable")
    manifest["proof_event"] = record["event_id"]
    changed = current_dependencies(project, record, version)
    if changed is not None:
        return unknown(changed, invalidated=True)
    meta = project.version(version)
    if payload["rom_sha1"] != meta.baserom_sha1 or (rom_size is not None and size != rom_size):
        return unknown("data.rom.identity", invalidated=True)
    if (
        meta.baserom.is_file()
        and inputs.digest(meta.baserom, algorithm="sha1", reuse=cache.configured()) != payload["rom_sha1"]
    ):
        return unknown("data.rom.changed", invalidated=True)
    matched: dict[tuple[int, int], str] = {}
    observed: dict[tuple[int, int], tuple[str, str]] = {}
    pinned_sources = {"/".join(pin.path.parts) for pin in attempts.dependency_record(record["dependencies"]).files}
    definitions: dict[Path, dict[str, str]] = {}
    for evidence in payload["evidence"]:
        if not isinstance(evidence, dict) or set(evidence) != {
            "final_artifact_sha256",
            "input_identity",
            "sections",
            "extents",
            "unavailable_reason",
        }:
            refuse("complete final linked evidence required")
        sha(evidence["input_identity"], "input_identity")
        if evidence["unavailable_reason"] is not None and not isinstance(evidence["unavailable_reason"], str):
            refuse("unavailable_reason must be text or null")
        if not isinstance(evidence["sections"], list) or not isinstance(evidence["extents"], list):
            refuse("sections/extents arrays required")
        if evidence["final_artifact_sha256"] is None:
            if evidence["extents"]:
                refuse("verified extents without final linked artifact")
            manifest["causes"].append({"key": "data.final.unavailable", "reason": evidence["unavailable_reason"]})
            continue
        sha(evidence["final_artifact_sha256"], "final_artifact_sha256")
        sections = {}
        for section in evidence["sections"]:
            if not isinstance(section, dict) or set(section) != {"name", "address", "size", "sha256"}:
                refuse("complete emitted section required")
            if not isinstance(section["name"], str) or section["name"] in sections:
                refuse("unique emitted section name required")
            integer(section["address"], "section.address")
            integer(section["size"], "section.size", positive=True)
            sha(section["sha256"], "section.sha256")
            if section["name"].lstrip(".") in {"bss", "sbss"}:
                refuse("BSS is outside ROM data")
            sections[section["name"]] = section
        for extent in evidence["extents"]:
            if not isinstance(extent, dict) or set(extent) != {
                "version",
                "owner_source",
                "symbol",
                "section",
                "address",
                "rom_offset",
                "size",
                "source_sha256",
                "bytes_sha256",
                "rom_bytes_sha256",
                "definition_proof_id",
            }:
                refuse("complete verified source extent required")
            start = integer(extent["rom_offset"], "rom_offset")
            length = integer(extent["size"], "size", positive=True)
            address = integer(extent["address"], "address")
            end = start + length
            if (
                extent["version"] != version
                or end > size
                or not isinstance(extent["symbol"], str)
                or not extent["symbol"]
            ):
                refuse("invalid verified extent bounds/version/symbol")
            sha(extent["source_sha256"], "source_sha256")
            byte_hash = sha(extent["bytes_sha256"], "bytes_sha256")
            rom_hash = sha(extent["rom_bytes_sha256"], "rom_bytes_sha256")
            sha(extent["definition_proof_id"], "definition_proof_id")
            owner = extent["owner_source"]
            if not isinstance(owner, dict) or set(owner) != {"root", "parts"} or not isinstance(owner["parts"], list):
                refuse("portable source identifier required")
            logical = inputs.LogicalPath(owner["root"], tuple(owner["parts"]))
            path = project.root.joinpath(*logical.parts)
            if logical.root != "project" or not path.is_relative_to(project.src) or path.suffix != ".c":
                refuse("current project C/source owner required")
            key = (start, end)
            prior = observed.get(key)
            if prior is not None and prior != (byte_hash, rom_hash):
                refuse("conflicting aliased byte evidence")
            observed[key] = (byte_hash, rom_hash)
            unit = path.relative_to(project.src).with_suffix("").as_posix()
            source = sources.get(unit)
            if path.relative_to(project.root).as_posix() not in pinned_sources:
                refuse("source absent from native dependency proof")
            section = sections.get(extent["section"])
            row = next((row for row in intervals if row.start <= start and end <= row.end), None)
            if (
                section is None
                or not section["address"] <= address < address + length <= section["address"] + section["size"]
            ):
                refuse("extent outside final emitted section")
            if row is None or row.address is None or address != row.address + start - row.start:
                refuse("extent outside declared ROM mapping")
            if address == section["address"] and length == section["size"] and byte_hash != section["sha256"]:
                refuse("extent and final section byte hashes differ")
            key = (start, end)
            if key in matched and matched[key] != byte_hash:
                refuse("conflicting aliased byte evidence")
            if source is None or source[0] != extent["source_sha256"] or source[2]:
                manifest["causes"].append({"key": "data.source.unavailable", "source": logical.name, "bytes": length})
                continue
            if byte_hash != rom_hash:
                manifest["causes"].append({"key": "data.bytes.mismatch", "start": start, "end": end})
                continue
            if path not in definitions:
                from unbake.project.headers import Graph

                definitions[path] = Graph.capture(project).initialized_definitions(project, path, version)
            if definitions[path].get(extent["symbol"]) != extent["definition_proof_id"]:
                manifest["causes"].append(
                    {"key": "data.definition.unavailable", "source": logical.name, "bytes": length}
                )
                continue
            matched[key] = byte_hash
            manifest["extents"].append(
                {
                    "start": start,
                    "end": end,
                    "source": logical.name,
                    "symbol": extent["symbol"],
                    "proof": extent["definition_proof_id"],
                }
            )
        if evidence["unavailable_reason"] is not None:
            manifest["causes"].append({"key": "data.ownership.unavailable", "reason": evidence["unavailable_reason"]})
    spans = sorted(observed)
    if any(left[1] > right[0] for left, right in pairwise(spans)):
        refuse("overlapping emitted data extents")
    union = tuple(sorted(matched))
    if any(left[1] > right[0] for left, right in pairwise(union)):
        refuse("overlapping verified extents")
    credit = sum(end - start for start, end in union)
    if not 0 <= credit <= total:
        refuse("matched_data outside declared denominator")
    manifest.update(
        verified_bytes=credit,
        unmeasured_bytes=total - credit,
        state="verified" if credit == total and total else "partial" if credit else "unmeasured",
    )
    if credit < total:
        manifest["causes"].append({"key": "data.coverage.unmeasured", "unmeasured_bytes": total - credit})
    return Coverage(intervals, union, manifest)
