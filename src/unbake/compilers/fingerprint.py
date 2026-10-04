"""Compiler families from instructions and releases from compiled probes."""

import re
import struct
import tempfile
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path

from unbake.layout.split import Function
from unbake.compilers.propose import (
    confirm_proposal as confirm_proposal,
)
from unbake.compilers.propose import (
    propose_compilers as propose_compilers,
)
from unbake.compilers.propose import (
    receipt as receipt,
)
from unbake.config import Held, Host, Project
from unbake.project.rom import Rom, shingles
from unbake.compilers.registry import CompilerSpec
from unbake.project_tools import atomic as atomic_files


@dataclass(frozen=True)
class Counts:
    addu: int = 0
    or_: int = 0

    @property
    def family(self) -> str | None:
        total = self.addu + self.or_
        if total >= 8:
            if self.addu / total >= 0.8:
                return "gcc"
            if self.or_ / total >= 0.8:
                return "ido"
        return None


@dataclass(frozen=True)
class Region:
    start: int
    end: int
    family: str | None
    counts: Counts
    name: str
    functions: tuple[Function, ...] = ()

    @property
    def key(self) -> str:
        return f"0x{self.start:08X}-0x{self.end:08X}"


@dataclass(frozen=True)
class Decision:
    id: str | None
    scores: dict[str, int]
    probes: tuple[str, ...]
    matches: dict[str, tuple[bool, ...]]
    errors: dict[str, str] = field(default_factory=dict)
    comparable: tuple[str, ...] = ()


def _counts(data: bytes) -> Counts:
    addu = or_ = 0
    for (word,) in struct.iter_unpack(">I", data):
        if word >> 26 or (word >> 6) & 31 or not (word >> 11) & 31:
            continue
        rs, rt = (word >> 21) & 31, (word >> 16) & 31
        if bool(rs) == bool(rt):
            continue
        if word & 63 == 0x21:
            addu += 1
        elif word & 63 == 0x25:
            or_ += 1
    return Counts(addu, or_)


def idioms(rom: Rom, ranges: Iterable[tuple[int, int]]) -> dict[tuple[int, int], Counts]:
    result = {}
    image = rom.image()
    for start, end in ranges:
        if not 0 <= start < end <= len(image) or start % 4 or end % 4:
            raise Held("setup", f"idioms ROM range 0x{start:X}-0x{end:X}: invalid word range")
        result[start, end] = _counts(image[start:end])
    return result


def regions(functions: Iterable[Function], rom: Rom) -> list[Region]:
    """Classify instruction copies per function, then coalesce adjacent families."""
    ordered = sorted(functions, key=lambda function: function.start)
    if not ordered:
        raise Held("setup", f"{rom.path}: splat found no functions")
    measured = idioms(rom, [(function.start, function.end) for function in ordered])
    families = []
    for function in ordered:
        count = measured[function.start, function.end]
        total = count.addu + count.or_
        family = None
        if total and count.addu / total >= 0.8:
            family = "gcc"
        elif total and count.or_ / total >= 0.8:
            family = "ido"
        families.append(family)
    image = rom.image()
    groups: list[tuple[str | None, list[Function]]] = []
    for function, family in zip(ordered, families, strict=True):
        if groups and groups[-1][0] == family and groups[-1][1][-1].end == function.start:
            groups[-1][1].append(function)
        else:
            groups.append((family, [function]))
    names: dict[str, int] = {}
    output: list[Region] = []
    for family, members in groups:
        base = "main" if family == "gcc" else family or "undecided"
        names[base] = names.get(base, 0) + 1
        name = base if names[base] == 1 else f"{base}_{members[0].address:08X}"
        counts = [_counts(image[function.start : function.end]) for function in members]
        output.append(
            Region(
                members[0].address,
                members[-1].address + members[-1].end - members[-1].start,
                family,
                Counts(sum(count.addu for count in counts), sum(count.or_ for count in counts)),
                name,
                tuple(members),
            )
        )
    return output


def evidence(rom: Rom) -> list[str]:
    clues = [f"header libultra field: 0x{rom.header.libultra:08X} (evidence only)"]
    marker = re.compile(rb"(?i)(?:PSYQ\.H|\blibultra\b|\bKMC\b|\b(?:GCC|GNU)\b.{0,48}\d+\.\d+)")
    for match in re.finditer(rb"[\x20-\x7e]{4,}", rom.image()):
        if marker.search(match[0]):
            clues.append(f"ROM 0x{match.start():X}: {match[0][:160].decode('ascii')} (evidence only)")
    return clues


def confirm(
    reference: list[Region], reference_rom: Rom, functions: Iterable[Function], rom: Rom, version: str
) -> list[Region]:
    """Map families by function shingles, refusing conflicting matching functions."""
    observed = regions(functions, rom)
    index: dict[frozenset[bytes], set[str | None]] = {}
    reference_image, image = reference_rom.image(), rom.image()
    for region in reference:
        for function in region.functions:
            key = shingles(reference_image[function.start : function.end])
            if key:
                index.setdefault(key, set()).add(region.family)
    for region in observed:
        for function in region.functions:
            families = index.get(shingles(image[function.start : function.end]))
            if families is not None and region.family not in families:
                raise Held(
                    "init",
                    f"region {region.key} VERSION {version}: "
                    f"family {region.family} disagrees with reference {families}",
                )
    return observed


def probe(function: Function, data: bytes) -> bool:
    if not 8 <= (function.end - function.start) // 4 <= 60:
        return False
    for (word,) in struct.iter_unpack(">I", data[function.start : function.end]):
        opcode = word >> 26
        if (
            opcode in (1, 3, 4, 5, 6, 7, 20, 21, 22, 23)
            or (opcode == 0 and word & 63 == 9)
            or (opcode == 17 and word >> 21 & 31 == 8)
        ):
            return True
    return False


def _body(path: Path, function: str) -> tuple[bytes, dict[int, int]]:
    """Read a named ELF32 MIPS body and masks from its relocation sections."""
    data = path.read_bytes()
    if data[:6] != b"\x7fELF\x01\x02" or len(data) < 52:
        raise Held("setup", f"{path}: expected ELF32 big-endian object")
    header = struct.unpack_from(">16sHHIIIIIHHHHHH", data)
    if header[2] != 8:
        raise Held("setup", f"{path}: expected MIPS object")
    offset, size, count = header[6], header[11], header[12]
    sections = [struct.unpack_from(">10I", data, offset + index * size) for index in range(count)]
    symbols = []
    for section in sections:
        if section[1] != 2:
            continue
        strings = sections[section[6]]
        table = data[strings[4] : strings[4] + strings[5]]
        for index in range(section[4], section[4] + section[5], section[9]):
            name, address, length, info, _, section_index = struct.unpack_from(">IIIBBH", data, index)
            label = table[name : table.find(b"\0", name)].decode()
            if 0 < section_index < len(sections):
                symbols.append((label, address, length, info, section_index))
    definitions = [symbol for symbol in symbols if symbol[0] == function]
    if len(definitions) != 1:
        raise Held("setup", f"{path}: expected one defined symbol {function}")
    _, start, length, _, section_index = definitions[0]
    section = sections[section_index]
    if not section[2] & 4:
        raise Held("setup", f"{path}: {function} is not executable")
    end = (
        start + length
        if length
        else min(
            [
                symbol[1]
                for symbol in symbols
                if symbol[4] == section_index and symbol[1] > start and symbol[3] >> 4 != 0
            ]
            + [section[5]]
        )
    )
    body = data[section[4] + start : section[4] + end]
    masks = {}
    kinds = {0: 0, 1: 0xFFFF, 2: 0xFFFFFFFF, 4: 0x03FFFFFF, 5: 0xFFFF, 6: 0xFFFF, 7: 0xFFFF, 10: 0xFFFF}
    for relocation in sections:
        if relocation[1] not in (9, 4) or relocation[7] != section_index:
            continue
        for index in range(relocation[4], relocation[4] + relocation[5], relocation[9]):
            address, info = struct.unpack_from(">II", data, index)
            kind = info & 255
            if kind not in kinds:
                raise Held("setup", f"{path}: unsupported MIPS relocation {kind}")
            if start <= address < end:
                masks[address - start] = kinds[kind]
    return body, masks


def reproduces(path: Path, function: str, expected: bytes) -> bool:
    produced, masks = _body(path, function)
    if len(produced) != len(expected):
        return False
    for offset in range(0, len(expected), 4):
        target = int.from_bytes(expected[offset : offset + 4], "big")
        candidate = int.from_bytes(produced[offset : offset + 4], "big")
        if (target ^ candidate) & ~masks.get(offset, 0):
            return False
    return bool(expected)


def choose(
    matches: dict[str, tuple[bool, ...]], probes: tuple[str, ...], errors: dict[str, str] | None = None
) -> Decision:
    """A winner needs exclusive matches and no exclusive counterexample."""
    if any(len(results) != len(probes) for results in matches.values()):
        raise Held("setup", "setup.compiler_proposal: probe result denominator differs")
    failures = errors or {}
    comparable = tuple(
        index
        for index, name in enumerate(probes)
        if name not in failures and not any(f"{ident}/{name}" in failures for ident in matches)
    )
    winners = []
    for candidate, results in matches.items():
        others = [scores for ident, scores in matches.items() if ident != candidate]
        exclusive = any(results[index] and all(not other[index] for other in others) for index in comparable)
        counterexample = any(not results[index] and any(other[index] for other in others) for index in comparable)
        if exclusive and not counterexample:
            winners.append(candidate)
    return Decision(
        winners[0] if len(winners) == 1 else None,
        {ident: sum(results[index] for index in comparable) for ident, results in matches.items()},
        probes,
        matches,
        failures,
        tuple(probes[index] for index in comparable),
    )


def prove(project_scratch: Project, region: Region, candidates: Sequence[CompilerSpec], policy: Host) -> Decision:
    from unbake.decomp import m2c
    from unbake.compilers import registry as toolchain
    from unbake.project import build
    from unbake.project.rom import load

    if not candidates:
        raise Held("setup", f"region {region.key}: compiler candidates missing")
    if type(getattr(policy, "probe_count", None)) is not int or policy.probe_count <= 0:
        raise Held("setup", "policy.probe_count: required positive integer")
    project = project_scratch
    policy.m2c
    compilers = dict(project.compilers)
    for candidate in candidates:
        if candidate.id not in compilers:
            raise Held("setup", f"compilers.{candidate.id}: probe compiler is not installed")
        toolchain.verify(project.tools / candidate.id, candidate)
    project = replace(project, compilers=compilers)
    version = project.names_from
    data = load(project.version(version).baserom).image()
    probes = sorted(
        (function for function in region.functions if probe(function, data)),
        key=lambda function: (function.end - function.start, function.start),
    )[: policy.probe_count]
    results: dict[str, list[bool]] = {candidate.id: [] for candidate in candidates}
    errors = {}
    probe_root = project.build / "setup/probes"
    probe_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".probes-", dir=probe_root) as temporary:
        work = Path(temporary)
        include = work / "include"
        include.mkdir()
        atomic_files.text(
            include / "probe.h",
            "typedef s32 M2C_UNK;\ntypedef s8 M2C_UNK8;\ntypedef s16 M2C_UNK16;\n"
            "typedef s32 M2C_UNK32;\ntypedef s64 M2C_UNK64;\n"
            "#define M2C_FIELD(value, pointer_type, offset) (*(pointer_type)((s8 *)(value) + (offset)))\n",
        )
        project = replace(project, include=(*project.include, include))
        for function in probes:
            try:
                drafting = replace(
                    project, default_compiler=candidates[0].id, units={**project.units, function.name: candidates[0].id}
                )
                source = m2c.draft(drafting, policy, function.name, version, work)
            except Held as error:
                errors[function.name] = f"{error.phase}: {error.reason}"
                for values in results.values():
                    values.append(False)
                continue
            for candidate in candidates:
                try:
                    selected = replace(
                        project, default_compiler=candidate.id, units={**project.units, function.name: candidate.id}
                    )
                    out = work / candidate.id / (function.name + ".o")
                    build.compile_object(selected, policy, source, version, out)
                    identical = reproduces(out, function.name, data[function.start : function.end])
                except (Held, OSError, ValueError, struct.error) as error:
                    errors[f"{candidate.id}/{function.name}"] = str(error)
                    identical = False
                results[candidate.id].append(identical)
    return choose(
        {ident: tuple(values) for ident, values in results.items()}, tuple(function.name for function in probes), errors
    )
