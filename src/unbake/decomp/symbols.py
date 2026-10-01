"""Derive data bindings from MIPS relocations and return native Splat edits."""

from __future__ import annotations

from collections.abc import Sequence
from importlib import import_module
from typing import TYPE_CHECKING, TypeVar

if TYPE_CHECKING:
    from unbake.decomp.trial_artifacts import TrialContext
import re
import struct
from dataclasses import dataclass

from unbake.decomp.needs import LabelNeed, Need, SymbolNeed, register_deriver
from unbake.families import Family, family_for
from unbake.families.mips import relocation_value
from unbake.project.config import Held


@dataclass(frozen=True)
class Binding:
    name: str
    address: int
    section: str
    type: str
    size: int


@dataclass(frozen=True)
class DataRow:
    name: str
    start: int
    end: int
    section: str


@dataclass(frozen=True)
class Relocation:
    offset: int
    kind: int
    name: str


@dataclass(frozen=True)
class TrialElf:
    words: tuple[int, ...]
    relocations: tuple[Relocation, ...]
    bindings: tuple[Binding, ...]
    rows: tuple[DataRow, ...]
    gp: int | None
    family: Family
    settled: frozenset[int]


@dataclass(frozen=True)
class Reference:
    address: int
    type: str
    size: int
    offset: int


_LOADS = {
    0x20: ("s8", 1),
    0x21: ("s16", 2),
    0x23: ("s32", 4),
    0x24: ("u8", 1),
    0x25: ("u16", 2),
    0x28: ("s8", 1),
    0x29: ("s16", 2),
    0x2B: ("s32", 4),
    0x31: ("f32", 4),
    0x35: ("f64", 8),
    0x37: ("s64", 8),
    0x39: ("f32", 4),
    0x3D: ("f64", 8),
    0x3F: ("s64", 8),
}
_NAME = r"[A-Za-z_.$][\w.$]*"


T = TypeVar("T")


def required(value: T | None, name: str) -> T:
    if value is None or value == "":
        raise Held("symbols", f"{name}: missing value")
    return value


def _signed(value: int) -> int:
    return (value & 0x7FFF) - (value & 0x8000)


def references(target_words: Sequence[int], gp: int | None) -> list[Reference]:
    """Find absolute memory accesses whose base is proved by constant instructions."""
    required(target_words, "target_words")
    constants = {0: 0}
    if gp is not None:
        constants[28] = gp
    result: list[Reference] = []
    reset_after_slot = False
    for index, word in enumerate(target_words):
        reset_now, reset_after_slot = reset_after_slot, False
        if type(word) is not int or not 0 <= word <= 0xFFFFFFFF:
            raise Held("symbols", f"target_words[{index}]: expected unsigned word")
        op, rs, rt = word >> 26, word >> 21 & 31, word >> 16 & 31
        immediate = word & 0xFFFF
        if op in _LOADS and rs in constants:
            address = (constants[rs] + _signed(immediate)) & 0xFFFFFFFF
            if address >= 0x80000000:
                type_, size = _LOADS[op]
                result.append(Reference(address, type_, size, index * 4))
        if op == 15 and rt:
            constants[rt] = immediate << 16
        elif op in (9, 13) and rt:
            if rs in constants:
                constants[rt] = (
                    (constants[rs] + _signed(immediate)) if op == 9 else constants[rs] | immediate
                ) & 0xFFFFFFFF
            else:
                constants.pop(rt, None)
        elif op == 0:
            rd, function = word >> 11 & 31, word & 63
            if function in (0x21, 0x25) and rd and rs in constants and rt in constants:
                constants[rd] = (
                    (constants[rs] + constants[rt]) if function == 0x21 else constants[rs] | constants[rt]
                ) & 0xFFFFFFFF
            elif rd:
                constants.pop(rd, None)
        elif op in (2, 3) or op in (1, 4, 5, 6, 7, 0x14, 0x15, 0x16, 0x17):
            reset_after_slot = True
        elif op not in (0x28, 0x29, 0x2B, 0x39, 0x3D, 0x3F, 0x31, 0x35, 0x11) and rt:
            constants.pop(rt, None)
        if op == 0 and word & 63 in (8, 9):
            reset_after_slot = True
        if reset_now:
            constants = {0: 0, **({28: gp} if gp is not None else {})}
    return result


def _row(rows: Sequence[DataRow], address: int, size: int, name: str) -> DataRow:
    found = [row for row in rows if row.start <= address < row.end]
    if len(found) != 1:
        raise Held("symbols", f"{name}: data row at 0x{address:08X} is missing or ambiguous")
    row = found[0]
    required(row.section, f"{name}.section")
    if address + size > row.end:
        raise Held("symbols", f"{name}.size: crosses data row {row.name}")
    return row


def _bindings(bindings: Sequence[Binding]) -> dict[str, Binding]:
    result: dict[str, Binding] = {}
    for binding in bindings:
        if binding.name in result and result[binding.name] != binding:
            raise Held("symbols", f"{binding.name}: two-addresses or conflicting metadata")
        result[binding.name] = binding
    return result


def _need(
    version: str,
    name: str,
    address: int,
    addend: int,
    type_: str,
    size: int,
    rows: Sequence[DataRow],
    bindings: dict[str, Binding],
    evidence: str,
) -> list[Need]:
    existing = bindings.get(name)
    if existing and existing.address != address:
        raise Held("symbols", f"{name}: placed-elsewhere at 0x{existing.address:08X}, inferred 0x{address:08X}")
    row = _row(rows, address, size, name)
    needs: list[Need] = [SymbolNeed(version, name, address, addend, row.section, type_, size, evidence)]
    if row.start < address and not existing:
        needs.append(LabelNeed(version, name, address, row.name, evidence))
    return needs


def derive(
    trial_elf: TrialElf,
    target_words: Sequence[int],
    version: str,
    declared: dict[str, int] | None = None,
    aligned: Sequence[int] | None = None,
) -> list[Need]:
    """Bind relocation symbols using paired target immediates and object addends."""
    required(version, "version")
    required(trial_elf, "trial_elf")
    required(target_words, "target_words")
    bindings = _bindings(trial_elf.bindings)
    access = {ref.offset: ref for ref in references(target_words, trial_elf.gp) if ref.address not in trial_elf.settled}
    family = required(trial_elf.family, "trial_elf.family")
    relocations = [
        relocation
        for relocation in trial_elf.relocations
        if not (relocation.name in bindings and bindings[relocation.name].section == ".text")
    ]
    grouped: dict[tuple[int, int, str], tuple[Relocation, list[Relocation]]] = {}
    for high, low in family.relocation_pairs(relocations):
        key = (low.offset, low.kind, low.name)
        grouped.setdefault(key, (low, []))[1].extend([high] if high is not None else [])
    result: list[Need] = []
    observed: dict[str, int] = {}
    for relocation, highs in grouped.values():
        name = required(relocation.name, "relocation.name")
        binding = bindings.get(name)
        offset, kind = relocation.offset, relocation.kind
        if kind in (0, 4, 10):
            continue
        if declared is not None and name in declared:
            _, addend = relocation_value(highs, relocation, trial_elf.words, trial_elf.words, trial_elf.gp)
            effective = (declared[name] + addend) & 0xFFFFFFFF
        else:
            effective, addend = relocation_value(
                highs, relocation, trial_elf.words, target_words if aligned is None else aligned, trial_elf.gp
            )
        address = (effective - addend) & 0xFFFFFFFF
        if name in observed and observed[name] != address:
            raise Held("symbols", f"{name}: two-addresses")
        observed[name] = address
        if address in trial_elf.settled and (binding is None or binding.address == address):
            continue
        ref = access.get(offset)
        if aligned is not None and ref is not None and ref.address != effective:
            ref = None
        if ref:
            type_, size = ref.type, ref.size
        elif binding:
            type_, size = binding.type, binding.size
        else:
            # An address materialization can be typed by a later proved access.
            related = [ref for ref in access.values() if ref.address == effective]
            # A relocation proves placement even when only the address is used.
            type_, size = (related[0].type, related[0].size) if related else ("address", 0)
        if binding:
            type_, size = binding.type, binding.size
        required(type_, f"{name}.type")
        if size <= 0 and (type_, size) != ("address", 0):
            raise Held("symbols", f"{name}.size: expected positive extent")
        evidence = f"relocation {kind} at +0x{offset:X}; reference 0x{effective:08X}; addend {addend}"
        result.extend(_need(version, name, address, addend, type_, size, trial_elf.rows, bindings, evidence))
    # Constant references without relocations still expose missing mid-interval labels.
    for ref in access.values():
        if any(need.address + need.addend == ref.address for need in result if isinstance(need, SymbolNeed)):
            continue
        exact = [binding for binding in bindings.values() if binding.address == ref.address]
        name = exact[0].name if len(exact) == 1 else f"D_{ref.address:08X}"
        result.extend(
            _need(
                version,
                name,
                ref.address,
                0,
                ref.type,
                ref.size,
                trial_elf.rows,
                bindings,
                f"constant reference at +0x{ref.offset:X}",
            )
        )
    unique: dict[tuple[type[Need], str, int], Need] = {}
    for need in result:
        assert isinstance(need, (SymbolNeed, LabelNeed))
        need_key = (type(need), need.name, need.address)
        if need_key in unique and isinstance(need, SymbolNeed):
            previous = unique[need_key]
            assert isinstance(previous, SymbolNeed)
            if need.type == "address":
                continue
            if previous.type == "address":
                unique[need_key] = need
                continue
            if (previous.type, previous.size) != (need.type, need.size):
                raise Held("symbols", f"{need.name}.type: conflicting access evidence")
        unique[need_key] = need
    return list(unique.values())


def symbol_line(need: SymbolNeed) -> str:
    """Render the exact Splat symbol declaration used in edits and guidance."""
    for field in ("version", "name", "address", "section", "type", "size"):
        required(getattr(need, field), f"{need.name}.{field}")
    if not re.fullmatch(_NAME, need.name):
        raise Held("symbols", f"{need.name}.name: invalid symbol")
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", need.type):
        raise Held("symbols", f"{need.name}.type: invalid Splat type")
    address_only = (need.type, need.size) == ("address", 0)
    if (
        not 0 <= need.address <= 0xFFFFFFFF
        or (need.size <= 0 and not address_only)
        or (need.type == "address" and not address_only)
    ):
        raise Held("symbols", f"{need.name}.address/size: invalid range")
    if address_only:
        return f"{need.name} = 0x{need.address:08X};"
    return f"{need.name} = 0x{need.address:08X}; // type:{need.type} size:0x{need.size:X}"


def derive_trial(context: TrialContext) -> list[Need]:
    """Consume TrialContext artifacts and add exact declarations to try comparisons."""
    from unbake.decomp.guide import data_rows, render, words
    from unbake.decomp.trial_compare import align_words
    from unbake.decomp.trial_layout import symbol_values
    from unbake.project_tools.elf import Object

    result = []
    for version, artifact in context.artifacts.items():
        for field in ("unit", "target_words", "version"):
            if field not in artifact:
                raise Held("symbols", f"artifacts.{version}.{field}: missing value")
        try:
            obj = Object(artifact["unit"].path)
            text = obj.section(".text")
            text = required(text, f"{version}.text")
            draft = words(obj.content(text), "big")
            pending = obj.relocations(text)
        except (OSError, ValueError, IndexError, struct.error) as error:
            raise Held("symbols", f"{version}.trial_elf: {error}") from error
        rows = data_rows(context.project, version)
        values = symbol_values(artifact["version"].symbols)
        refs = references(artifact["target_words"], values.get("_gp"))
        # Declared addresses outside data rows are settled by the symbol file; object-local
        # sections are owned by the constant-pool deriver.
        settled = {name for name, address in values.items() if not any(row.start <= address < row.end for row in rows)}
        relocations = tuple(
            Relocation(offset, kind, symbol["name"])
            for offset, kind, symbol in pending
            if not symbol["section"] and symbol["name"] not in settled
        )
        # Out-of-row accesses have no label to declare, including fields of a settled base.
        pooled = {offset for offset, _, symbol in pending if symbol["section"]}
        placed = {values[name] for name in settled} | {
            ref.address
            for ref in refs
            if ref.offset in pooled or not any(row.start <= ref.address < row.end for row in rows)
        }
        bindings = []
        for name, address in values.items():
            matches = [ref for ref in refs if ref.address == address]
            containing = [row for row in rows if row.start <= address < row.end]
            if matches and len(containing) == 1:
                bindings.append(Binding(name, address, containing[0].section, matches[0].type, matches[0].size))
        family = family_for(context.project.compiler_for(context.source).id)
        # Declared symbols link at their established addresses. Their exact relocated
        # words are checked after linking, rather than inferred again at draft offsets.
        alignment = align_words(
            artifact["target_words"],
            draft,
            {offset // 4: mask for offset, mask in artifact["unit"].relocations.get(".text", {}).items()},
        )
        aligned = [0] * len(draft)
        matched = set()
        for tag, left, right, start, stop in alignment:
            if tag == "equal":
                for i, j in zip(range(left, right), range(start, stop), strict=True):
                    aligned[j] = artifact["target_words"][i]
                    matched.add(j)
        for relocation in relocations:
            if (
                relocation.name not in values
                and relocation.kind not in (0, 4, 10)
                and relocation.offset // 4 not in matched
            ):
                raise Held(
                    "symbols",
                    f"{relocation.name}: relocation instruction differs: "
                    f"no aligned evidence at +0x{relocation.offset:X}",
                )
        object_evidence = TrialElf(
            draft, relocations, tuple(bindings), rows, values.get("_gp"), family, frozenset(placed)
        )
        derived = derive(object_evidence, artifact["target_words"], version, values, aligned)
        result.extend(derived)
        guidance = render(derived)
        if guidance:
            context.trial.compares[version].lines.extend(guidance.splitlines())
    return result


register_deriver(derive_trial)
import_module("unbake.decomp.symbols_edits")
