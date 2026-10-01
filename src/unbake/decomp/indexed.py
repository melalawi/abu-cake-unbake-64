"""Constant anchors of scaled indexed MIPS memory references."""

import struct
from collections.abc import Sequence
from dataclasses import dataclass

from unbake.decomp.trial_layout import FunctionSpan, rom_reader
from unbake.layout import split
from unbake.project.config import Held, Project


@dataclass(frozen=True)
class IndexedReference:
    address: int
    offset: int
    scale: int


def indexed_references(target_words: Sequence[int]) -> list[IndexedReference]:
    """Track an anchor plus a scaled unknown index without claiming scalar identity."""
    constants = {0: 0}
    scaled: dict[int, int] = {}
    indexed: dict[int, tuple[int, int]] = {}
    result = []
    reset_after_slot = False
    for index, word in enumerate(target_words):
        if type(word) is not int or not 0 <= word <= 0xFFFFFFFF:
            raise Held("symbols", f"target_words[{index}]: expected unsigned word")
        reset_now, reset_after_slot = reset_after_slot, False
        op, rs, rt = word >> 26, word >> 21 & 31, word >> 16 & 31
        rd, function = word >> 11 & 31, word & 63
        immediate = word & 0xFFFF
        signed = (immediate & 0x7FFF) - (immediate & 0x8000)
        if op in (0x20, 0x21, 0x23, 0x24, 0x25, 0x31, 0x35, 0x37) and rs in indexed:
            anchor, scale = indexed[rs]
            address = (anchor + signed) & 0xFFFFFFFF
            if address >= 0x80000000:
                result.append(IndexedReference(address, index * 4, scale))
        destination = rd if op == 0 else rt
        constant = None
        scale_value = None
        anchor_value = None
        if op == 15:
            constant = immediate << 16
        elif op in (9, 13) and rs in constants:
            constant = (constants[rs] + signed if op == 9 else constants[rs] | immediate) & 0xFFFFFFFF
        elif op == 0 and function == 0 and rt not in constants and (word >> 6 & 31):
            scale_value = 1 << (word >> 6 & 31)
        elif op == 0 and function == 0x21:
            if rs in constants and rt in constants:
                constant = (constants[rs] + constants[rt]) & 0xFFFFFFFF
            elif rs in constants and rt in scaled:
                anchor_value = constants[rs], scaled[rt]
            elif rt in constants and rs in scaled:
                anchor_value = constants[rt], scaled[rs]
        writes = op == 0 or op in (*range(8, 16), 0x18, 0x19, *range(0x20, 0x28), 0x30, 0x34, 0x37)
        if writes and destination:
            constants.pop(destination, None)
            scaled.pop(destination, None)
            indexed.pop(destination, None)
            if constant is not None:
                constants[destination] = constant
            if scale_value is not None:
                scaled[destination] = scale_value
            if anchor_value is not None:
                indexed[destination] = anchor_value
        if op in (1, 2, 3, 4, 5, 6, 7, 0x14, 0x15, 0x16, 0x17) or (op == 0 and function in (8, 9)):
            reset_after_slot = True
        if reset_now:
            constants, scaled, indexed = {0: 0}, {}, {}
            if scale_value is not None and destination:
                scaled[destination] = scale_value
    return result


def table_guidance(project: Project, version: str, function: str, span: FunctionSpan, target: Sequence[int]) -> str:
    """Name ROM-backed indexed tables whose complete entries target this function."""
    configured = project.version(version)
    read_memory = rom_reader(configured)
    _, _, segments = split.layout(configured.split)
    lines = []
    for ref in indexed_references(target):
        if ref.scale != 4:
            continue
        for segment in segments:
            for row in segment.rows:
                start = split.address(row, configured.split)
                end = start + split.end(row) - row.start
                if row.kind.lstrip(".") not in ("data", "rodata", "rdata") or not start <= ref.address < end:
                    continue
                cursor = ref.address
                while cursor + 4 <= end:
                    entry = struct.unpack(">I", read_memory(cursor, 4))[0]
                    if entry % 4 or not span.address <= entry < span.address + span.size:
                        break
                    cursor += 4
                if cursor == ref.address:
                    continue
                lines.append(
                    f"jump table: {function} owns jtbl_{ref.address:08X}; {row.kind} table "
                    f"0x{ref.address:08X}; ROM offset 0x{row.start + ref.address - start:X}; "
                    f"size 0x{cursor - ref.address:X}; resident {row.path}"
                )
    return "\n".join(dict.fromkeys(lines))
