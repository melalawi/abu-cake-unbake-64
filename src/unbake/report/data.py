"""Source data coverage from dependency-bound final-link evidence in the existing Ledger."""

from __future__ import annotations

import hashlib
import json
import re
import struct
from dataclasses import asdict, dataclass
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


@attempts.with_ledger
def snapshots(project: Project) -> dict[str, dict[str, Any] | None]:
    history = attempts.ledger(project)
    history._refresh()
    latest = {}
    for identity_ in history.order:
        event = history.events[identity_]
        if event["kind"] == "native.data":
            latest[event["subject"]] = event
    projections = {
        event["subject"]: event for event in history.events.values() if event["kind"] == "native.data.inputs"
    }
    result: dict[str, dict[str, Any] | None] = {}
    for version in project.versions:
        producers = [event for subject, event in latest.items() if subject.startswith(version + "/")]
        legacy = latest.get(version)
        if producers:
            result[version] = {"legacy": legacy, "producers": producers}
            if relevant := [projections[event["event_id"]] for event in producers if event["event_id"] in projections]:
                combined = result[version]
                assert combined is not None
                combined["input_projections"] = relevant
        else:
            result[version] = legacy
    return result


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
    from unbake.project.headers import Graph, scan

    graph = Graph.capture(project)
    closure = graph.closure((source,), command)

    def portable(path: inputs.LogicalPath) -> inputs.LogicalPath:
        return inputs.LogicalPath("project", path.parts) if path.root == graph.view.root_id else path

    pins = {
        portable(pin.path): inputs.FilePin(
            portable(pin.path),
            pin.state,
            pin.sha256,
            portable(pin.link_target) if pin.link_target is not None else None,
        )
        for pin in closure.dependency_set.files
    }
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
            "dependencies_unknown": any(
                include.unknown for path in (source, *closure.paths) for include in scan(graph.read(path).decode())
            ),
        },
        {
            **closure.dependency_set.recipes,
            "data.emission": inputs.digest(Path(__file__), algorithm="sha256", reuse=cache.configured()),
        },
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
            if index is None:
                index = obj.section(item.name)
            if index is None or obj.sections[index][3] != item.address or obj.sections[index][5] != item.size:
                refuse("final initialized section missing or differs from native placement")
            emitted = obj.content(index)
            if obj.sections[index][1] != 1 or not obj.sections[index][2] & 2 or obj.relocations(index):
                refuse("final initialized section is not allocated, relocated PROGBITS")
            sections.append(
                {
                    "name": obj.names[index],
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
                        "section": obj.names[index],
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
    payload: dict[str, Any] = {
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


def producer_binding(project: Project, version: str, unit: Any) -> dict[str, Any]:
    """The owning default-build binding, including storage and resource execution addresses."""
    from unbake import buildfiles

    if unit.kind == "data":
        path, section = buildfiles.data_bindings(project, version)[unit]
    else:
        path = buildfiles.resource_bindings(project, version)[unit]
        section = "resource"
    binding: dict[str, Any] = json.loads(attempts.encoded({"unit": asdict(unit), "source": path, "section": section}))
    return binding


def canonical_configuration(value: dict[str, Any]) -> dict[str, Any]:
    """Generated build-root spelling is host layout, while recipe flags remain semantic."""
    return {
        **value,
        "unit_recipe": [
            re.sub(r"build/%[./]+(?=(?:src|units|data|resources)/)", "build/%/", line)
            for line in value.get("unit_recipe", [])
        ],
    }


def producer_configuration(project: Project, version: str, binding: dict[str, Any]) -> dict[str, Any]:
    """Scope mutable project configuration to this native producer, not unrelated inventory."""
    from unbake.compilers import drivers

    name = binding["unit"]["name"]
    return {
        "rom_sha1": project.version(version).baserom_sha1,
        "binding": binding,
        "compiler": project.compiler_reference(name) if binding["unit"]["kind"] == "data" else None,
        "flags": drivers.flags(project, version, name) if binding["unit"]["kind"] == "data" else [],
        "cppflags": list(project.cppflags),
        "asflags": list(project.gnu_asflags),
        "original_asflags": list(project.asflags),
        "unit_recipe": [
            line
            for line in (
                (project.root / "units.mk").read_text().splitlines() if (project.root / "units.mk").is_file() else []
            )
            if "src/" + name + "." in line or "data/" + name + "." in line
        ],
    }


def capture_producer(
    project: Project, version: str, unit: Any, linked: bytes, original: bytes
) -> dict[str, Any] | None:
    """Retain an accepted standalone native output, never infer credit from its source row.

    Existing Make freshness and native/ROM admission supply the proof. A historical
    output older than its actual producer inputs remains unmeasured; no build is run.
    """
    from unbake import buildfiles
    from unbake.compilers import drivers
    from unbake.objects.elf import Object
    from unbake.project.headers import Graph, scan

    binding = producer_binding(project, version, unit)
    source = project.root / binding["source"]
    native = project.build_link(version) / ("data" if unit.kind == "data" else "resources") / (unit.name + ".bin")
    if linked != original or len(linked) != unit.size or not native.is_file():
        return None
    graph = Graph.capture(project)
    if unit.kind == "data":
        command = drivers.flags(project, version, unit.name)
        closure = graph.closure((source,), command)
        if any(include.unknown for path in (source, *closure.paths) for include in scan(graph.read(path).decode())):
            return None
        producer_paths = {source, *closure.paths}
        script = project.root / "versions" / version / (project.name + ".data.ld")
    else:
        producer_paths = {source, *buildfiles.resource_inputs(project)}
        # Include every resource source: assembler includes may nest or use CPP.
        producer_paths.update((project.root / "resources").rglob("*.s"))
        script = project.root / "versions" / version / "resources" / (unit.name + ".ld")
    # Generated inventory can be rewritten by unrelated code publication. Its
    # own binding/flags/recipe are checked semantically below; source/header
    # freshness belongs to the accepted native producer, not that rewrite.
    freshness_paths = set(producer_paths)
    producer_paths.update(project.root / name for name in ("Makefile", "units.mk", "tools/compilers.sha256"))
    producer_paths.add(project.root / "versions" / version / "symbols.ld")
    if unit.kind == "data" or unit.assembler == "gnu":
        producer_paths.add(script)
    if any(not path.is_file() or path.is_symlink() for path in producer_paths):
        return None
    if any(path.stat().st_mtime_ns > native.stat().st_mtime_ns for path in freshness_paths):
        return None
    meta = project.version(version)
    if inputs.digest(meta.baserom, algorithm="sha1", reuse=cache.configured()) != meta.baserom_sha1:
        return None
    paths = producer_paths | {project.root / name for name in required(project, version)} | {meta.baserom}
    dependencies = inputs.DependencySet(
        tuple(inputs.file_pin(path, root=project.root, root_id="project", reuse=False) for path in sorted(paths)),
        {
            "version": version,
            "dependencies_unknown": False,
            "producer_binding": binding,
            "producer_configuration": producer_configuration(project, version, binding),
        },
        {"data.producer": inputs.digest(Path(__file__), algorithm="sha256", reuse=cache.configured())},
    )
    if current_dependencies(project, {"dependencies": dependencies.document()}, version) is not None:
        return None
    source_hash = inputs.digest(source, algorithm="sha256", reuse=False)
    byte_hash = hashlib.sha256(linked).hexdigest()
    sections = [
        {
            "name": ".data" if unit.kind == "data" else ".resource",
            "address": unit.address,
            "size": unit.size,
            "sha256": byte_hash,
        }
    ]
    bounds = []
    artifact_hash = byte_hash
    if unit.kind == "data":
        final = native.with_suffix(".elf")
        if not final.is_file():
            return None
        obj = Object(final)
        index = obj.section(".data")
        if index is None:
            index = obj.section(".rodata")
        if (
            struct.unpack_from(">H", obj.data, 16)[0] != 2
            or index is None
            or obj.sections[index][1] != 1
            or not obj.sections[index][2] & 2
            or obj.sections[index][3] != unit.address
            or obj.content(index) != linked
            or obj.relocations(index)
        ):
            return None
        sections[0]["name"] = obj.names[index]
        sizes: dict[str, int] = {}
        try:
            definitions = graph.initialized_definitions(project, source, version, sizes=sizes)
        except Held as error:
            if error.key != "data.definition":
                raise
            # Unsupported declaration evidence is local to this producer. The
            # existing native admission still owns byte equality and hygiene.
            return None
        named = {
            symbol["name"]
            for table in obj.symbols.values()
            for symbol in table
            if symbol["section"] == index and symbol["name"] and symbol["info"] & 15 != 3
        }
        for table in obj.symbols.values():
            for symbol in table:
                name = symbol["name"]
                length = symbol["size"] or sizes.get(name, 0)
                # SN64 emits STT_NOTYPE size-zero composite definitions. The
                # explicit standalone binding owns the entire native section
                # only when it has exactly one initialized definition at its base.
                if not length and named == {name} and name in definitions and symbol["value"] == unit.address:
                    length = unit.size
                offset = symbol["value"] - unit.address
                if (
                    name in definitions
                    and symbol["section"] == index
                    and length > 0
                    and 0 <= offset < offset + length <= unit.size
                ):
                    bounds.append((name, offset, length, definitions[name]))
        artifact_hash = inputs.digest(final, algorithm="sha256", reuse=False)
    else:
        from unbake.decomp import checks

        if checks.resource_opcodes(source) or checks.findings(project, (source,), cache.Cache(project.cache)).rows:
            return None
        bounds.append((unit.name, 0, unit.size, digest(binding)))
    extents = []
    for name, offset, length, proof in bounds:
        hashed = hashlib.sha256(linked[offset : offset + length]).hexdigest()
        extents.append(
            {
                "version": version,
                "owner_source": {"root": "project", "parts": list(source.relative_to(project.root).parts)},
                "symbol": name,
                "section": sections[0]["name"],
                "address": unit.address + offset,
                "rom_offset": unit.start + offset,
                "size": length,
                "source_sha256": source_hash,
                "bytes_sha256": hashed,
                "rom_bytes_sha256": hashed,
                "definition_proof_id": proof,
            }
        )
    if not extents:
        return None
    payload: dict[str, Any] = {
        "schema": 1,
        "version": version,
        "rom_sha1": meta.baserom_sha1,
        "rom_size": meta.baserom.stat().st_size,
        "evidence": [
            {
                "final_artifact_sha256": artifact_hash,
                "input_identity": dependencies.digest,
                "sections": sections,
                "extents": extents,
                "unavailable_reason": None,
                "producer": binding,
            }
        ],
    }
    projection = linker_inputs(
        project, {"dependencies": dependencies.document(), "result": {"value": {"native_data": payload}}}
    )
    if projection is not None:
        dependencies = inputs.DependencySet(
            dependencies.files, {**dependencies.values, "producer_linker_inputs": projection}, dependencies.recipes
        )
        payload["evidence"][0]["input_identity"] = dependencies.digest
    assert_inputs(project, dependencies)
    return {
        "subject": version + "/" + unit.kind + "/" + unit.name,
        "dependencies": dependencies.document(),
        "payload": payload,
    }


@attempts.with_ledger
def record_producers(project: Project, proofs: list[dict[str, Any]]) -> list[str]:
    """Append one immutable event per changed accepted producer, retaining all earlier history."""
    history = attempts.ledger(project)
    written = []
    for proof in proofs:
        dependencies = attempts.dependency_record(proof["dependencies"])
        assert_inputs(project, dependencies)
        prior = history.latest("native.data", proof["subject"])
        payload = proof["payload"]
        if (
            prior is not None
            and prior["result"]["value"].get("native_data") == payload
            and digest(prior["dependencies"]) == digest(dependencies.document())
        ):
            continue
        operation = attempts.Operation.make(project, "native.data", proof["subject"], {}, dependencies)
        written.append(
            history.record(
                operation,
                attempts.Outcome(operation.id, "ok", {"native_data": payload}, None, {}, (digest(payload),)),
                parents=(prior["event_id"],) if prior is not None else (),
            )
        )
    return written


def linker_references(
    source: Path, flags: list[str], texts: dict[Path, str]
) -> tuple[set[str], set[str], set[str]] | None:
    """The existing conservative macro/reference projection, shared with admission."""
    if any("##" in flag for flag in flags):
        return None
    identifiers: set[str] = set(re.findall(r"\b[A-Za-z_]\w*\b", " ".join(flags)))
    prefixes: set[str] = set()
    suffixes: set[str] = set()
    macros: dict[str, list[tuple[set[str], str]]] = {}
    for path, text in texts.items():
        text = text.replace("\\\n", "")
        text = re.sub(r"//[^\n]*|/\*.*?\*/|\"(?:\\.|[^\"\\])*\"|'(?:\\.|[^'\\])*'", " ", text, flags=re.S)
        directive = re.compile(r"^\s*#\s*define\s+(\w+)(\([^)]*\))?([^\n]*)", re.M)
        for macro in directive.finditer(text):
            parameters = set(re.findall(r"\w+", macro[2] or ""))
            macros.setdefault(macro[1], []).append((parameters, macro[3]))
        body = directive.sub("", text)
        if path == source or path.suffix in {".s", ".inc"}:
            identifiers.update(re.findall(r"\b[A-Za-z_]\w*\b", body))
        else:
            # Provider declarations do not create linker references. Retain
            # initialized provider bodies conservatively; expand used macros below.
            for initializer in re.findall(r"=[^;]+;", body):
                identifiers.update(re.findall(r"\b[A-Za-z_]\w*\b", initializer))
        if path.suffix in {".s", ".inc"} and "\\" in text:
            return None
    pending = list(identifiers)
    expanded: set[str] = set()
    while pending:
        name = pending.pop()
        if name in expanded:
            continue
        expanded.add(name)
        for parameters, body in macros.get(name, []):
            for paste in re.finditer(r"\b(?=(\w+)\s*##\s*(\w+))", body):
                left, right = paste.groups()
                if left in parameters and right in parameters:
                    return None
                if left not in parameters:
                    prefixes.add(left)
                if right not in parameters:
                    suffixes.add(right)
            tokens = set(re.findall(r"\b[A-Za-z_]\w*\b", body)) - parameters
            pending.extend(tokens - identifiers)
            identifiers.update(tokens)
    return identifiers, prefixes, suffixes


def linker_symbols(path: Path, text: str) -> dict[str, int] | None:
    """The same strict symbol/alias and PROVIDE grammar for both projections."""
    from unbake import buildfiles

    if path.name == "symbols.ld":
        pattern = r"PROVIDE\(([A-Za-z_]\w*)\s*=\s*(0x[0-9A-Fa-f]+|[0-9]+)\);"
        if re.sub(pattern, "", text).strip():
            return None
        provided = re.findall(pattern, text)
        if len({name for name, _ in provided}) != len(provided):
            return None
        return {name: int(address, 0) for name, address in provided}
    _, rows = split.parse_symbols(path, text)
    symbols = {name: address for name, (address, _, _) in rows.items()}
    for name, address in buildfiles._LINKER_ALIAS.findall(text):
        symbols.setdefault(name, int(address, 16))
    return symbols


def referenced_symbols(
    references: tuple[set[str], set[str], set[str]], symbols: dict[str, int]
) -> dict[str, int]:
    identifiers, prefixes, suffixes = references
    relevant = identifiers & symbols.keys()
    relevant.update(name for name in symbols if name.startswith(tuple(prefixes)) or name.endswith(tuple(suffixes)))
    return {name: symbols[name] for name in sorted(relevant)}


def linker_inputs(
    project: Project, event: dict[str, Any], *, archive_root: Path | None = None
) -> dict[str, Any] | None:
    """Project only symbols spelled by the hash-bound producer and header closure.

    An archive projection verifies the original bytes against the original pins.
    It describes those inputs; it never changes a captured dependency or proof hash.
    """
    dependencies = attempts.dependency_record(event["dependencies"])
    binding = dependencies.values.get("producer_binding")
    if binding is None:
        return None
    root = archive_root or project.root
    configuration = dependencies.values.get("producer_configuration", {})
    flags = [*configuration.get("flags", []), *configuration.get("cppflags", [])]
    texts = {}
    pins = {pin.path.name: pin for pin in dependencies.files}
    for pin in dependencies.files:
        if pin.path.root != "project":
            return None
        path = root.joinpath(*pin.path.parts)
        include_roots = [root / directory.relative_to(project.root) for directory in project.include]
        if path.suffix not in {".c", ".h", ".s", ".inc"} and not any(
            path.is_relative_to(directory) for directory in [root / "src", root / "resources", *include_roots]
        ):
            continue
        if pin.state != "file" or not path.is_file() or path.is_symlink():
            return None
        content = path.read_bytes()
        if hashlib.sha256(content).hexdigest() != pin.sha256:
            if archive_root is not None:
                refuse("archived producer input differs from its captured pin")
            return None
        texts[path] = content.decode()
    references = linker_references(root / binding["source"], flags, texts)
    if references is None:
        return None
    identifiers, _, _ = references
    payload = event["result"]["value"]["native_data"]
    # Native definitions resolve their own names; PROVIDE rows for those
    # definitions are unused. Binding/address checks still own the extent.
    identifiers.difference_update(
        extent["symbol"] for evidence in payload["evidence"] for extent in evidence["extents"]
    )
    version = payload["version"]
    names = (project.version(version).symbols.relative_to(project.root).as_posix(), f"versions/{version}/symbols.ld")
    projected = {}
    for name in names:
        linker_pin = pins.get("project:" + name)
        if linker_pin is None or linker_pin.state != "file":
            return None
        path = root / name
        if not path.is_file() or path.is_symlink():
            return None
        text = path.read_text()
        if archive_root is not None and hashlib.sha256(path.read_bytes()).hexdigest() != linker_pin.sha256:
            refuse("archived linker input differs from its captured pin")
        symbols = linker_symbols(path, text)
        if symbols is None:
            return None
        projected[name] = {
            "captured_sha256": linker_pin.sha256,
            "symbols": referenced_symbols(references, symbols),
        }
    return projected


@attempts.with_ledger
def record_input_projections(project: Project, archive_root: Path) -> list[str]:
    """Append hash-verified semantic readbacks of preserved producer inputs."""
    history = attempts.ledger(project)
    history._refresh()
    written = []
    for identity_ in tuple(history.order):
        event = history.events[identity_]
        if event["kind"] != "native.data" or "producer_binding" not in event["dependencies"]["values"]:
            continue
        if event["dependencies"]["values"].get("producer_linker_inputs") is not None:
            continue
        projection = linker_inputs(project, event, archive_root=archive_root)
        if projection is None:
            continue
        value = {
            "proof_event": identity_,
            "input_identity": attempts.dependency_record(event["dependencies"]).digest,
            "linker_inputs": projection,
        }
        prior = history.latest("native.data.inputs", identity_)
        if prior is not None and prior["result"]["value"] == value:
            continue
        dependencies = inputs.DependencySet(
            (),
            {"input_identity": value["input_identity"]},
            {"data.input_projection": inputs.digest(Path(__file__), algorithm="sha256", reuse=cache.configured())},
        )
        operation = attempts.Operation.make(project, "native.data.inputs", identity_, {}, dependencies)
        written.append(
            history.record(
                operation, attempts.Outcome(operation.id, "ok", value, None, {}, (digest(value),)), parents=(identity_,)
            )
        )
    return written


def link_tools(host: Host) -> dict[str, str]:
    return {
        name: inputs.digest(Path(getattr(host, name)), algorithm="sha256", reuse=cache.configured())
        for name in ("mips_ld", "mips_objcopy", "n64link")
    }


def current_dependencies(project: Project, event: dict[str, Any], version: str) -> str | None:
    dependencies = attempts.dependency_record(event["dependencies"])
    if dependencies.values.get("dependencies_unknown") or not dependencies.recipes:
        return "data.dependencies.unknown"
    binding = dependencies.values.get("producer_binding")
    semantic_paths: set[str] = set()
    if binding is not None:
        if canonical_configuration(dependencies.values.get("producer_configuration", {})) != canonical_configuration(
            producer_configuration(project, version, binding)
        ):
            return "data.configuration.changed"
        semantic_paths = {
            "config.toml",
            "layout.toml",
            "units.mk",
            project.version(version).split.relative_to(project.root).as_posix(),
        }
    projection = dependencies.values.get("producer_linker_inputs")
    if projection is None and "event_id" in event:
        certificate = attempts.ledger(project).latest("native.data.inputs", event["event_id"])
        if certificate is not None:
            value = certificate["result"]["value"]
            if (
                certificate["result"]["state"] == "ok"
                and event["event_id"] in certificate["parents"]
                and value.get("proof_event") == event["event_id"]
                and value.get("input_identity") == dependencies.digest
                and digest(value) in certificate["result"]["proof_ids"]
            ):
                projection = value.get("linker_inputs")
    if projection is not None:
        if not isinstance(projection, dict) or not projection or projection != linker_inputs(project, event):
            return "data.linker_inputs.changed"
        semantic_paths.update(projection)
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
        if (
            path.relative_to(project.root).as_posix() not in semantic_paths
            and inputs.digest(path, algorithm="sha256", reuse=cache.configured()) != pin.sha256
        ):
            return "data.dependencies.changed"
    if not required(project, version) <= pinned:
        return "data.dependencies.incomplete"
    return None


@attempts.with_ledger
def coverage(
    project: Project, version: str, sources: dict[str, tuple[str, set[str], bool]], record: dict[str, Any] | None
) -> Coverage:
    if record is not None and {"legacy", "producers"} <= set(record) <= {"legacy", "producers", "input_projections"}:
        records = [event for event in [record["legacy"], *record["producers"]] if event is not None]
        pieces = [coverage(project, version, sources, event) for event in records]
        empty = coverage(project, version, sources, None)
        combined_spans = sorted({span for piece in pieces for span in piece.matched})
        if any(left[1] > right[0] for left, right in pairwise(combined_spans)):
            refuse("overlapping producer data extents")
        extents = [extent for piece in pieces for extent in piece.manifest["extents"]]
        hashes: dict[tuple[int, int], str] = {}
        for extent in extents:
            span = (extent["start"], extent["end"])
            if span in hashes and hashes[span] != extent["bytes_sha256"]:
                refuse("conflicting producer byte evidence")
            hashes[span] = extent["bytes_sha256"]
        credit = sum(end - start for start, end in combined_spans)
        total = empty.manifest["declared_bytes"]
        combined_manifest = {
            **empty.manifest,
            "evidence_identity": digest(record),
            "proof_event": [event["event_id"] for event in records],
            "verified_bytes": credit,
            "unmeasured_bytes": total - credit,
            "extents": extents,
            "invalidations": sum(piece.manifest["invalidations"] for piece in pieces),
            "causes": [
                cause
                for piece in pieces
                for cause in piece.manifest["causes"]
                if cause["key"] != "data.coverage.unmeasured"
            ],
            "state": "verified" if credit == total and total else "partial" if credit else "unmeasured",
        }
        if credit < total:
            combined_manifest["causes"].append({"key": "data.coverage.unmeasured", "unmeasured_bytes": total - credit})
        return Coverage(empty.intervals, tuple(combined_spans), combined_manifest)
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
        if not isinstance(evidence, dict) or set(evidence) - {"producer"} != {
            "final_artifact_sha256",
            "input_identity",
            "sections",
            "extents",
            "unavailable_reason",
        }:
            refuse("complete final linked evidence required")
        producer = evidence.get("producer")
        if producer is not None:
            from unbake import buildfiles

            bindings = [
                producer_binding(project, version, unit)
                for unit in [
                    *buildfiles.data_bindings(project, version),
                    *buildfiles.resource_bindings(project, version),
                ]
            ]
            if producer not in bindings or record["dependencies"]["values"].get("producer_binding") != producer:
                return unknown("data.binding.changed", invalidated=True)
        resource = producer is not None and producer["unit"]["kind"] == "resource"
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
            if logical.root != "project" or (
                (
                    resource
                    and producer is not None
                    and (
                        "/".join(logical.parts) != producer["source"]
                        or not path.is_relative_to(project.root / "resources")
                    )
                )
                or (
                    not resource
                    and producer is not None
                    and (not path.is_relative_to(project.src) or path.suffix != ".c")
                )
            ):
                refuse("current project producer source owner required")
            if producer is not None and "/".join(logical.parts) != producer["source"]:
                refuse("extent owner differs from producer binding")
            key = (start, end)
            prior = observed.get(key)
            if prior is not None and prior != (byte_hash, rom_hash):
                refuse("conflicting aliased byte evidence")
            observed[key] = (byte_hash, rom_hash)
            unit = path.relative_to(project.src).with_suffix("").as_posix() if not resource else ""
            source = (
                sources.get(unit)
                if not resource
                else (inputs.digest(path, algorithm="sha256", reuse=cache.configured()), set(), False)
                if path.is_file()
                else None
            )
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

                definitions[path] = (
                    {extent["symbol"]: digest(producer)}
                    if resource
                    else Graph.capture(project).initialized_definitions(project, path, version)
                )
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
                    "source": "/".join(logical.parts),
                    "bytes_sha256": byte_hash,
                    "symbol": extent["symbol"],
                    "proof": extent["definition_proof_id"],
                    "kind": "resource" if resource else "data",
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
