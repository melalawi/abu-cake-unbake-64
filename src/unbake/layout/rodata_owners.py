"""Produce inspectable resident constant ownership without writing build inputs."""

import bisect
import hashlib
import itertools
import re
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from pathlib import Path

from unbake.config import Held, Project
from unbake.decomp.rom import project_reader
from unbake.layout import split
from unbake.layout.rodata_references import Reference, collect, words
from unbake.objects.elf import Object
from unbake.objects.rodata import pools, relocated, table_addresses, table_pointer_bias


@dataclass(frozen=True)
class Span:
    address: int
    start: int
    end: int
    bias: int
    scope: str

    @property
    def stop(self) -> int:
        return self.address + self.end - self.start


@dataclass
class Constant:
    address: int
    end: int
    kind: str
    scope: str
    proof: str
    names: list[str]
    table_destinations: list[str] = field(default_factory=list)
    owners: set[str] = field(default_factory=set)
    writes: set[str] = field(default_factory=set)
    data_references: set[str] = field(default_factory=set)
    rom_pointer_candidates: list[int] = field(default_factory=list)

    @property
    def safe_sole_candidate(self) -> bool:
        return (
            len(self.owners) == 1
            and self.kind != "other"
            and not self.writes
            and not self.data_references
            and not self.rom_pointer_candidates
        )

    def document(self) -> dict[str, object]:
        return {
            **asdict(self),
            "owners": sorted(self.owners),
            "writes": sorted(self.writes),
            "data_references": sorted(self.data_references),
            "size": self.end - self.address,
            "safe_sole_candidate": self.safe_sole_candidate,
        }


@dataclass
class Census:
    version: str
    objects: list[Constant]
    references: list[Reference]
    errors: list[str]
    spans: list[Span]
    snapshot: dict[str, str]
    functions: list[split.Function]

    def document(self) -> dict[str, object]:
        return {
            "version": self.version,
            "objects": [item.document() for item in self.objects],
            "references": [asdict(item) for item in self.references],
            "errors": self.errors,
            "spans": [asdict(span) for span in self.spans],
            "snapshot": self.snapshot,
            "functions": [asdict(f) for f in self.functions],
        }


def mapped_spans(project: Project, version: str, *, include_data: bool) -> list[Span]:
    """One constant span per runtime byte, preserving configured resident pointer identity."""
    reader = project_reader(project, version)
    spans = [
        Span(m.address, m.offset, m.offset + m.end - m.address, m.table_entry_bias, "resident")
        for m in reader.resident_spans()
    ]
    resident = tuple(spans)
    for span in resident:
        reader.table_span(span.address, span.end - span.start)
    for segment in reader.segments:
        for row in segment.rows:
            if row.kind.lstrip(".") not in (("data", "rodata", "rdata") if include_data else ("rodata", "rdata")):
                continue
            if "vram" not in segment.fields:
                continue
            address = split.address(row, project.version(version).split)
            stop = address + split.end(row) - row.start
            cuts = sorted({address, stop, *(at for s in resident for at in (s.address, s.stop) if address < at < stop)})
            for begin, end in itertools.pairwise(cuts):
                mapped = reader.table_span(begin, end - begin)
                if not any(s.address <= begin < end <= s.stop for s in resident):
                    start = mapped.offset + begin - mapped.address
                    spans.append(
                        Span(
                            begin,
                            start,
                            start + end - begin,
                            mapped.table_entry_bias,
                            "copied" if include_data else "strict",
                        )
                    )
    spans.sort(key=lambda s: s.address)
    if any(a.stop > b.address for a, b in itertools.pairwise(spans)):
        raise Held("rodata", "overlapping constant runtime spans")
    return spans


def scan(project: Project, version: str) -> Census:
    configured = project.version(version)
    image = configured.baserom.read_bytes()
    objects_dir = project.build / version / "src"
    _, configured_symbols = split.symbols(configured.symbols)
    values = {name: entry[0] for name, entry in configured_symbols.items()}
    spans = mapped_spans(project, version, include_data=False)
    functions = split.functions(project, version)
    refs: list[Reference] = []
    errors: list[str] = []
    compiler_tables: list[tuple[str, int, int]] = []
    for f in functions:
        owner = Path(f.path).stem
        # Published units have objects from make or compare; unmatched code is read from the ROM only.
        path = objects_dir / (Path(f.path).name + ".o")
        obj = Object(path) if f.kind == "c" and path.is_file() else None
        found, failures = collect(owner, image[f.start : f.end], obj, values.get("_gp"))
        refs.extend(r for r in found if any(span.address <= r.address < span.stop for span in spans))
        errors.extend(failures)
        if f.kind == "c" and obj is not None:
            try:
                compiler_tables.extend(proved_tables(obj, owner, image[f.start : f.end], f.address, image, spans))
            except ValueError as error:
                errors.append(f"{owner}: {error}")
    datarefs: dict[int, set[str]] = defaultdict(set)
    for path in sorted(objects_dir.glob("*.o")):
        obj = Object(path)
        for index, header in enumerate(obj.sections):
            if header[1] != 1 or not header[2] & 2 or obj.names[index] == ".text":
                continue
            material = obj.content(index)
            for offset, relocation_kind, symbol in obj.relocations(index):
                if relocation_kind == 2 and offset + 4 <= len(material) and symbol["name"] in values:
                    address = (
                        values[symbol["name"]] + int.from_bytes(material[offset : offset + 4], "big")
                    ) & 0xFFFFFFFF
                    if any(span.address <= address < span.stop for span in spans):
                        datarefs[address].add(path.relative_to(project.build).as_posix())
    objects = classify(image, functions, spans, refs, values, datarefs, compiler_tables)
    snapshot = {
        "rom_sha1": hashlib.sha1(image).hexdigest(),
        "split_sha256": hashlib.sha256(configured.split.read_bytes()).hexdigest(),
        "symbols_sha256": hashlib.sha256(configured.symbols.read_bytes()).hexdigest(),
    }
    return Census(version, objects, refs, errors, spans, snapshot, functions)


def private(census: Census, function: str) -> list[Constant]:
    return [o for o in census.objects if o.safe_sole_candidate and o.owners == {function}]


def material(project: Project, version: str, item: Constant) -> bytes:
    return project_reader(project, version)(item.address, item.end - item.address)


def proved_tables(
    obj: Object, owner: str, target: bytes, text_address: int, image: bytes, spans: list[Span]
) -> list[tuple[str, int, int]]:
    """Byte-check per-version C table extents before using them as ownership."""
    target_words = {at * 4: word for at, word in enumerate(words(target))}
    result = []
    for section in obj.names:
        explicit = re.fullmatch(r"\.unbake_(?:pool_([0-9A-F]{8})|piece_([0-9A-F]{8})_[0-9A-F]+)", section)
        if section not in (".rdata", ".rodata") and explicit is None:
            continue
        addresses = table_addresses(obj, section, target_words)
        runs = pools(obj, section, True)
        if explicit is not None:
            for pool in runs:
                anchor = int(explicit[1] or explicit[2], 16) + pool.offset
                if addresses.get(pool.offset, anchor) != anchor:
                    raise ValueError(f"{section}: table reference disagrees with explicit interval")
                addresses[pool.offset] = anchor
        if not addresses:
            continue
        material = relocated(obj, section, text_address)
        for pool in runs:
            if pool.offset not in addresses:
                continue
            address = addresses[pool.offset]
            mapped = [s for s in spans if s.address <= address < address + pool.size <= s.stop]
            if len(mapped) != 1:
                raise ValueError(f"{section}: table has no unique resident mapping at 0x{address:08X}")
            span = mapped[0]
            start = span.start + address - span.address
            raw = image[start : start + pool.size]
            normalized = b"".join(((word + span.bias) & 0xFFFFFFFF).to_bytes(4, "big") for word in words(raw))
            actual = material[pool.offset : pool.offset + pool.size]
            if len(raw) != pool.size or (actual != normalized and table_pointer_bias(actual, raw) is None):
                raise ValueError(f"{section}: table bytes disagree at 0x{address:08X}")
            result.append((owner, address, address + pool.size))
    return result


def classify(
    image: bytes,
    functions: list[split.Function],
    spans: list[Span],
    refs: list[Reference],
    values: dict[str, int] | None = None,
    datarefs: dict[int, set[str]] | None = None,
    compiler_tables: list[tuple[str, int, int]] | None = None,
) -> list[Constant]:
    """Partition every mapped constant byte using ROM-only or enriched evidence."""
    values = values or {}
    datarefs = datarefs or {}
    compiler_tables = compiler_tables or []
    table_ends: dict[int, int] = {}
    for _, address, end in compiler_tables:
        if address in table_ends and table_ends[address] != end:
            raise Held("rodata", f"conflicting compiler table extents at 0x{address:08X}")
        span = next((s for s in spans if s.address <= address < end <= s.stop), None)
        if span is None or address % 4 or (end - address) % 4:
            raise Held("rodata", f"compiler table outside aligned resident span at 0x{address:08X}")
        table_ends[address] = end
    if any(table_ends[a] > b for a, b in itertools.pairwise(sorted(table_ends))):
        raise Held("rodata", "overlapping compiler table extents")
    text = sorted(functions, key=lambda f: f.address)
    textstarts = [f.address for f in text]

    def text_at(address: int) -> str | None:
        at = bisect.bisect_right(textstarts, address) - 1
        f = text[at] if at >= 0 else None
        return Path(f.path).stem if f is not None and address < f.address + f.end - f.start else None

    def span_at(address: int) -> Span | None:
        return next((s for s in spans if s.address <= address < s.stop), None)

    def read(address: int, size: int) -> bytes:
        span = span_at(address)
        if span is None:
            return b""
        offset = span.start + address - span.address
        return image[offset : min(offset + size, span.end)]

    byaddr: dict[int, list[Reference]] = defaultdict(list)
    for ref in refs:
        byaddr[ref.address].append(ref)
    names: dict[int, list[str]] = defaultdict(list)
    for name, address in values.items():
        if span_at(address) is not None and ("D_" in name or name.startswith("jtbl_")):
            names[address].append(name)
    tables = set(table_ends)
    for address, rr in byaddr.items():
        span = span_at(address)
        assert span is not None
        entries = words(read(address, 8))
        local = len(entries) == 2 and all(text_at((w + span.bias) & 0xFFFFFFFF) or text_at(w) for w in entries)
        if (
            any(r.type == "indexed" and r.scale == 4 for r in rr)
            or any(n.startswith("jtbl_") for n in names[address])
            or (local and any(r.opcode in (9, 13) for r in rr))
        ):
            tables.add(address)
    tables.update(a for a, nn in names.items() if any(n.startswith("jtbl_") for n in nn))
    tableanchors = sorted(tables)
    objects: list[Constant] = []
    for span in spans:
        anchors = sorted(
            {
                span.address,
                span.stop,
                *(a for a in byaddr if span.address <= a < span.stop),
                *(a for a in datarefs if span.address <= a < span.stop),
                *(a for a in names if span.address <= a < span.stop),
                *(a for a in table_ends if span.address <= a < span.stop),
                *(a for a in table_ends.values() if span.address <= a < span.stop),
            }
        )
        typed: list[Constant] = []
        for address in anchors[:-1]:
            rr = byaddr[address]
            types = {r.type for r in rr if not r.write}
            size, kind, proof = 0, "other", "unclassified anchor interval"
            destinations: set[str] = set()
            if address in tables:
                following = bisect.bisect_right(tableanchors, address)
                cap = table_ends.get(
                    address, min(span.stop, tableanchors[following] if following < len(tableanchors) else span.stop)
                )
                for value in words(read(address, min(16384, cap - address))):
                    owner = text_at((value + span.bias) & 0xFFFFFFFF) or text_at(value)
                    if owner is None:
                        break
                    size += 4
                    destinations.add(owner)
                named = any(n.startswith("jtbl_") for n in names[address])
                if size >= 8 or (size and named):
                    kind = "jump table" if named or destinations <= {r.owner for r in rr} else "other"
                    proof = "local text pointer run capped at next table anchor"
                else:
                    size = 0
                if address in table_ends:
                    size = table_ends[address] - address
                    kind, proof = "jump table", "byte-proved compiler .rdata/.rodata relocations"
            if not size:
                if "f64" in types:
                    kind, size, proof = "double", 8, "f64 memory load"
                elif "f32" in types:
                    kind, size, proof = "float", 4, "f32 memory load"
                else:
                    raw = read(address, min(4096, span.stop - address))
                    end = raw.find(b"\0")
                    if (
                        span.scope != "strict"
                        and end >= 2
                        and all(x in (9, 10, 13) or 32 <= x < 127 for x in raw[:end])
                    ):
                        kind, size, proof = "string", end + 1, "printable NUL terminated reference anchor"
            if size:
                typed.append(
                    Constant(
                        address,
                        min(address + size, span.stop),
                        kind,
                        span.scope,
                        proof,
                        sorted(names[address]),
                        sorted(destinations),
                    )
                )
        cursor = span.address
        for item in [*typed, Constant(span.stop, span.stop, "other", span.scope, "", [])]:
            if item.address < cursor:
                continue
            gaps = [cursor, *(a for a in anchors if cursor < a < item.address), item.address]
            objects.extend(
                Constant(a, b, "other", span.scope, "unclassified anchor interval", sorted(names[a]))
                for a, b in itertools.pairwise(gaps)
                if a < b
            )
            if item.end > item.address:
                objects.append(item)
            cursor = item.end
    objects.sort(key=lambda o: o.address)
    starts = [o.address for o in objects]

    def object_at(address: int) -> Constant | None:
        at = bisect.bisect_right(starts, address) - 1
        return objects[at] if at >= 0 and address < objects[at].end else None

    for ref in refs:
        located = object_at(ref.address)
        if located is not None:
            located.owners.add(ref.owner)
            if ref.write:
                located.writes.add(ref.owner)
    for owner, address, _ in compiler_tables:
        located = object_at(address)
        assert located is not None
        located.owners.add(owner)
    for address, paths in datarefs.items():
        located = object_at(address)
        if located is not None:
            located.data_references.update(paths)
    for span in spans:
        for offset, value in enumerate(words(image[span.start : span.end])):
            located = object_at(value)
            if located is not None:
                located.rom_pointer_candidates.append(span.address + offset * 4)
    return objects
