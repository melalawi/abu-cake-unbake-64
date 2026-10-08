"""Relocation evidence and linker fragments for compiler constant sections."""

import re
import struct
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from itertools import pairwise

from unbake.compilers.families import constant_sections
from unbake.config import Held
from unbake.objects.elf import Object
from unbake.process import named as cause_named


@dataclass(frozen=True)
class Pool:
    section: str
    offset: int
    size: int


def pools(obj: Object, section: str, tables: bool) -> list[Pool]:
    """Partition a constant section into local-text pointer runs and literal bytes."""
    index = obj.section(section)
    if index is None:
        return []
    size = len(obj.content(index))
    text = obj.section(".text")
    entries = []
    for offset, kind, symbol in obj.relocations(index):
        if kind != 2 or text is None or symbol["section"] != text:
            raise ValueError(f"{section}.relocation[{offset}]: expected R_MIPS_32 against .text")
        if offset < 0 or offset % 4 or offset + 4 > size:
            raise ValueError(f"{section}.relocation[{offset}]: outside aligned section bytes")
        entries.append(offset)
    if len(set(entries)) != len(entries):
        raise ValueError(f"{section}.relocations: duplicate offsets")
    runs: list[tuple[int, int]] = []
    for offset in sorted(entries):
        if runs and runs[-1][1] == offset:
            runs[-1] = runs[-1][0], offset + 4
        else:
            runs.append((offset, offset + 4))
    if tables:
        return [Pool(section, start, end - start) for start, end in runs]
    result, cursor = [], 0
    for start, end in [*runs, (size, size)]:
        if start > cursor:
            result.append(Pool(section, cursor, start - cursor))
        cursor = end
    return result


def _word(data: bytes | bytearray, offset: int, label: str) -> int:
    if offset < 0 or offset % 4 or offset + 4 > len(data):
        raise ValueError(f"{label}[{offset}]: outside aligned word bytes")
    return int(struct.unpack_from(">I", data, offset)[0])


def signed(word: int) -> int:
    value = word & 0xFFFF
    return value - 0x10000 if value & 0x8000 else value


def placement(obj: Object, section: str, target_words: Mapping[int, int | None]) -> tuple[int, int]:
    """Return the unique plurality base and dissent count from aligned text words.

    target_words maps candidate byte offsets to matched target instruction words.
    An explicit None denotes an insertion with no target counterpart.
    """
    index, text = obj.section(section), obj.section(".text")
    if index is None:
        raise ValueError(f"{section}: missing section")
    if text is None:
        raise ValueError(".text: missing section")
    code = obj.content(text)
    pending: dict[tuple[int, int], list[tuple[int, int | None]]] = {}
    votes: Counter[int] = Counter()
    for offset, kind, symbol in obj.relocations(text):
        if symbol["section"] != index:
            continue
        if offset not in target_words:
            raise ValueError(f"target_words[{offset}]: missing aligned instruction")
        word, target = _word(code, offset, ".text"), target_words[offset]
        key = symbol["table"], symbol["index"]
        if kind == 5:
            pending.setdefault(key, []).append((word, target))
        elif kind == 6:
            highs = pending.pop(key, [])
            if not highs:
                raise ValueError(f"{section}.HI16[{offset}]: missing pair for {symbol['name']}")
            for high, original in highs:
                if target is None or original is None:
                    continue
                if high & 0xFFFF0000 != original & 0xFFFF0000 or word & 0xFFFF0000 != target & 0xFFFF0000:
                    continue
                own = ((high & 0xFFFF) << 16) + signed(word) + symbol["value"]
                address = ((original & 0xFFFF) << 16) + signed(target)
                votes[(address - own) & 0xFFFFFFFF] += 1
        else:
            raise ValueError(f"{section}.relocation[{offset}]: unsupported type {kind}")
    if pending:
        raise ValueError(f"{section}.LO16: missing pair for symbol {next(iter(pending))}")
    ranked = votes.most_common()
    if not ranked:
        raise ValueError(f"{section}.base: no relocated text reference")
    if len(ranked) > 1 and ranked[0][1] == ranked[1][1]:
        raise ValueError(f"{section}.base: tied relocation plurality")
    return ranked[0][0], sum(votes.values()) - ranked[0][1]


def relocated(
    obj: Object,
    section: str,
    text_address: int,
    *,
    symbols: Mapping[str, int] | None = None,
    section_addresses: Mapping[int, int] | None = None,
) -> bytes:
    """Materialize local jump-table pointers for comparison with resident bytes."""
    index, text = obj.section(section), obj.section(".text")
    if index is None:
        raise ValueError(f"{section}: missing section")
    result = bytearray(obj.content(index))
    for offset, kind, symbol in obj.relocations(index):
        if kind != 2:
            raise ValueError(f"{section}.relocation[{offset}]: expected local text pointer")
        if section_addresses is not None and symbol["section"] in section_addresses:
            target = section_addresses[symbol["section"]] + symbol["value"]
        elif symbols is not None and symbol["section"] == 0 and symbol["name"] in symbols:
            target = symbols[symbol["name"]]
        elif section_addresses is None and text is not None and symbol["section"] == text:
            target = text_address + symbol["value"]
        else:
            raise ValueError(f"{section}.relocation[{offset}]: missing proved symbol binding")
        value = _word(result, offset, section) + target
        struct.pack_into(">I", result, offset, value & 0xFFFFFFFF)
    return bytes(result)


def table_addresses(obj: Object, section: str, target_words: Mapping[int, int]) -> dict[int, int]:
    """Locate compiler table runs from their own instruction-identical references.

    Literal section order and another version's owners supply no evidence here.
    Callers must also prove the relocated table bytes against their ROM mapping.
    """
    index, text = obj.section(section), obj.section(".text")
    if index is None or text is None:
        return {}
    starts = {pool.offset for pool in pools(obj, section, True)}
    if not starts:
        return {}
    code = obj.content(text)
    pending: dict[tuple[int, int], list[int]] = {}
    result: dict[int, int] = {}
    for offset, kind, symbol in obj.relocations(text):
        if symbol["section"] != index:
            continue
        key = symbol["table"], symbol["index"]
        if kind == 5:
            pending.setdefault(key, []).append(offset)
        elif kind == 6:
            highs = pending.pop(key, [])
            if not highs:
                raise ValueError(f"{section}: table reference missing HI16")
            low = _word(code, offset, ".text")
            for at in highs:
                high = _word(code, at, ".text")
                own = ((high & 65535) << 16) + signed(low) + symbol["value"]
                if own not in starts:
                    continue
                original, target = target_words.get(at), target_words.get(offset)
                if original is None or target is None:
                    raise ValueError(f"{section}: missing aligned table reference at 0x{offset:X}")
                if (high ^ original) & 0xFFFF0000 or (low ^ target) & 0xFFFF0000:
                    raise ValueError(f"{section}: table reference instruction differs at 0x{offset:X}")
                address = (((original & 65535) << 16) + signed(target)) & 0xFFFFFFFF
                if own in result and result[own] != address:
                    raise ValueError(f"{section}: conflicting table placements")
                result[own] = address
        else:
            raise ValueError(f"{section}: unsupported table reference relocation {kind}")
    if pending:
        raise ValueError(f"{section}: table reference missing LO16")
    return result


def table_pointer_bias(actual: bytes, resident: bytes) -> int | None:
    """Prove the resident encoding of an already relocated local-text table.

    Some ROMs store physical text pointers and others store KSEG0 pointers.
    Require every relocated entry to agree exactly; this never maps pool bytes
    to a different address or admits an arbitrary pointer delta.
    """
    if not actual or len(actual) != len(resident) or len(actual) % 4:
        return None
    for bias in (0, 0x80000000):
        normalized = b"".join(
            struct.pack(">I", (word[0] + bias) & 0xFFFFFFFF) for word in struct.iter_unpack(">I", resident)
        )
        if normalized == actual:
            return bias
    return None


def fragment(rows: Iterable[Mapping[str, object]]) -> str:
    """Render proved shared-pool overlays from explicit split-row facts.

    Each row supplies object, section and address. Migrated local pools use the
    ordinary Splat selector; these overlays retain the resident ROM-producing row.
    """
    result = []
    for row in rows:
        for key in ("object", "section", "address"):
            if key not in row:
                raise ValueError(f"rodata.{key}: missing value")
        name, section, address = row["object"], row["section"], row["address"]
        if not isinstance(name, str) or not re.fullmatch(r"[\w./-]+\.o", name) or ".." in name.split("/"):
            raise ValueError("rodata.object: expected object path")
        if section not in constant_sections() and not (
            isinstance(section, str) and re.fullmatch(r"\.unbake_pool_[0-9A-F]{8}", section)
        ):
            raise ValueError("rodata.section: expected registered constant section")
        if isinstance(address, bool) or not isinstance(address, int) or not 0 <= address <= 0xFFFFFFFF:
            raise ValueError("rodata.address: expected 32-bit address")
        result.append(f"  .resident_{address:08X} 0x{address:08X} (NOLOAD) : SUBALIGN(1) {{ {name}({section}) }}")
    return "\n".join(result)


@dataclass(frozen=True)
class InitializedSection:
    name: str
    address: int
    rom_offset: int
    size: int
    symbols: tuple[tuple[str, int, int], ...]

    @property
    def output_name(self) -> str:
        return f".resident_{self.address:08X}{self.name}"


def initialized_sections(
    original: Object,
    placed: Object,
    windows: Iterable[tuple[int, int, int]],
    *,
    definition_sizes: Mapping[str, int] | None = None,
) -> tuple[InitializedSection, ...]:
    """Bind emitted object bounds to the native placer's proved absolute symbols.

    Anonymous pools and BSS do not supply initialized-definition ownership.
    A complete section, including compiler padding, must have one mapped base.
    Final relocated byte equality remains the link consumer's obligation.
    """
    absolute = {(symbol["table"], symbol["index"]): symbol for table in placed.symbols.values() for symbol in table}
    result = []
    mappings = tuple(windows)
    definition_sizes = definition_sizes or {}
    for index, (name, row) in enumerate(zip(original.names, original.sections, strict=True)):
        if (
            row[1] != 1
            or row[2] & 4
            or not row[5]
            or not (row[2] & 2 or name in constant_sections())
            or name == ".text"
        ):
            continue
        symbols = [
            symbol
            for table in original.symbols.values()
            for symbol in table
            if symbol["section"] == index
            and (
                (symbol["info"] & 15 == 1 and symbol["size"] > 0)
                or (symbol["info"] & 15 == 0 and symbol["name"] in definition_sizes)
            )
        ]
        if not symbols:
            continue
        if re.fullmatch(r"\.[A-Za-z0-9_.]+", name) is None:
            raise Held(cause_named("native.data.section", "unsafe section name", owner="objects.rodata", stage="data"))
        bases = set()
        bounds = []
        for symbol in symbols:
            size = symbol["size"] or definition_sizes.get(symbol["name"], 0)
            if size <= 0 or symbol["value"] + size > row[5]:
                raise Held(
                    cause_named(
                        "native.data.bounds",
                        f"{name}: symbol exceeds initialized section",
                        owner="objects.rodata",
                        stage="data",
                    )
                )
            target = absolute.get((symbol["table"], symbol["index"]))
            if (
                target is None
                or target["name"] != symbol["name"]
                or target["size"] != symbol["size"]
                or target["section"] != 0xFFF1
            ):
                continue
            bases.add(target["value"] - symbol["value"])
            bounds.append((symbol["name"], symbol["value"], size))
        if not bounds:
            continue
        if len(bases) != 1:
            raise Held(
                cause_named(
                    "native.data.placement",
                    f"{name}: conflicting native symbol placements",
                    owner="objects.rodata",
                    stage="data",
                )
            )
        address = bases.pop()
        offsets = {
            rom + address - vram for vram, rom, size in mappings if vram <= address and address + row[5] <= vram + size
        }
        if len(offsets) != 1:
            raise Held(
                cause_named(
                    "native.data.mapping",
                    f"{name}: missing or ambiguous complete ROM mapping",
                    owner="objects.rodata",
                    stage="data",
                )
            )
        result.append(InitializedSection(name, address, offsets.pop(), row[5], tuple(sorted(bounds))))
    for left, right in pairwise(sorted(result, key=lambda row: row.rom_offset)):
        if left.rom_offset + left.size > right.rom_offset:
            raise Held(
                cause_named("native.data.overlap", "initialized sections overlap", owner="objects.rodata", stage="data")
            )
    return tuple(result)


def insert_fragment(script: str, sections: str) -> str:
    """Insert compiler selectors before the discard rule in a Splat script."""
    if not sections:
        return script
    marker = re.search(r"^\s*/DISCARD/\s*:", script, re.M)
    if marker is None:
        raise ValueError("linker script: /DISCARD/ missing")
    return script[: marker.start()] + sections + "\n" + script[marker.start() :]


def unresolved_sections(obj: Object) -> list[str]:
    """Constant sections still reachable through relocations after native placement.

    Proved references are already absolute. Score mode can leave references to
    unplaced constants, including unallocated compiler literal pools. Follow their
    dependencies too, so a retained table never points into a discarded pool.
    """
    text = obj.section(".text")
    if text is None:
        return []
    pending, visited, retained = [text], {text}, set()
    while pending:
        for _, _, symbol in obj.relocations(pending.pop()):
            index = symbol["section"]
            if index in visited or not 0 < index < len(obj.sections):
                continue
            visited.add(index)
            section, name = obj.sections[index], obj.names[index]
            if section[1] not in (1, 8) or not section[5]:
                continue
            if not (section[2] & 2 or name in constant_sections()):
                continue
            retained.add(index)
            pending.append(index)
    return [obj.names[index] for index in sorted(retained)]


def trial_fragment(sections: Iterable[str], addresses: Mapping[str, int] | None = None) -> str:
    """Keep unplaced constants solely to link a nonexact score trial.

    These NOLOAD bytes never enter the extracted .text or replace ROM data.
    A section sits at zero unless addresses carries its aligned-reference base
    (aligned_bases); native placement still owns the exactness proof.
    """
    result = []
    for index, section in enumerate(sections):
        if not re.fullmatch(r"\.[\w.$-]+", section):
            raise ValueError(f"linker section: invalid name {section!r}")
        address = (addresses or {}).get(section)
        place = "0" if address is None else f"0x{address & 0xFFFFFFFF:08X}"
        result.append(f'  .trial_{index} {place} (NOLOAD) : SUBALIGN(1) {{ *("{section}") }}')
    return "\n".join(result)


def aligned_bases(obj: Object, sections: Iterable[str], target: bytes) -> dict[str, int]:
    """Score-mode base of each unplaced constant section, from its HI16/LO16 pairs.

    The native placer compares a reference with the ROM at the same text offset,
    so any inserted or deleted instruction before it leaves the section unplaced
    and its pair linking as lui 0. Align the candidate's words with the target's
    (relocated operand fields masked), then vote the section base from the pairs
    whose opcodes match. Only a unanimous section is returned; an ambiguous one
    stays out and links at zero.
    """
    from unbake.work.score import align_words

    text = obj.section(".text")
    if text is None or len(target) % 4:
        return {}
    code = obj.content(text)
    candidate = [int(word[0]) for word in struct.iter_unpack(">I", code)]
    reference = [int(word[0]) for word in struct.iter_unpack(">I", target)]
    masks = {
        offset // 4: 0xFFFF if kind in (5, 6) else 0x03FFFFFF
        for offset, kind, _ in obj.relocations(text)
        if kind in (4, 5, 6) and offset % 4 == 0 and offset // 4 < len(candidate)
    }
    matched: dict[int, int | None] = {offset: None for offset in masks}
    for tag, a, b, c, _ in align_words(reference, candidate, masks):
        if tag == "equal":
            for index in masks.keys() & set(range(c, c + b - a)):
                matched[index] = reference[a + index - c]
    result = {}
    for section in sections:
        try:
            base, dissent = placement(obj, section, {index * 4: word for index, word in matched.items()})
        except ValueError:
            continue
        if dissent == 0:
            result[section] = base
    return result


def defer_bss(script: str) -> str:
    """Move a premature Splat NOLOAD transition after its remaining load inputs.

    Local constant rows can make Splat insert automatic BSS before later text.
    The legacy writer switches to NOLOAD permanently at that first BSS entry.
    Preserve the load input order and place those BSS selectors with trailing BSS.
    """
    transition = re.compile(
        r"(?P<header>^    }\n"
        r"    (?P<name>\w+)_bss_VRAM = ADDR\(\.(?P=name)_bss\);\n"
        r"    \.(?P=name)_bss \(NOLOAD\)[^\n]*\n    \{\n"
        r"        FILL\([^\n]*\n        (?P=name)_BSS_START = \.;\n)"
        r"(?P<selectors>(?:        [^\n]+\(\.bss(?: COMMON)?\);\n)+)",
        re.M,
    )
    cursor = 0
    while match := transition.search(script, cursor):
        body_end = script.find("\n    }", match.end())
        if body_end < 0:
            raise ValueError("linker script: NOLOAD block terminator missing")
        body = script[match.end() : body_end]
        loaded = re.search(r"^        [^\n]+\(\.(?:text|data|rodata|rdata)\);", body, re.M)
        if loaded is None:
            cursor = match.end()
            continue
        tail = re.search(r"^        [^\n]+\(\.bss(?: COMMON)?\);", body, re.M)
        if tail is None:
            raise ValueError("linker script: trailing BSS selector missing")
        if tail.start() < loaded.start():
            raise ValueError("linker script: interleaved load and BSS inputs")
        script = (
            script[: match.start()]
            + body[: tail.start()]
            + match["header"]
            + match["selectors"]
            + body[tail.start() :]
            + script[body_end:]
        )
        cursor = match.start() + len(body[: tail.start()]) + len(match[0])
    return script
