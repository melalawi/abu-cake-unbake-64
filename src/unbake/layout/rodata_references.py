"""Read-only instruction and ELF evidence for resident constant ownership."""

import struct
from collections import defaultdict
from dataclasses import dataclass

from unbake.compilers.families.mips import Relocation, relocation_pairs
from unbake.config import Held
from unbake.decomp.indexed import indexed_references
from unbake.decomp.symbols import references
from unbake.objects.elf import Object
from unbake.objects.literal_layout import signed


@dataclass(frozen=True)
class Reference:
    owner: str
    address: int
    type: str
    offset: int
    opcode: int
    evidence: str
    symbol: str = ""
    scale: int = 0

    @property
    def write(self) -> bool:
        return self.opcode in (0x28, 0x29, 0x2B, 0x39, 0x3D, 0x3F)


def words(data: bytes) -> list[int]:
    return [item[0] for item in struct.iter_unpack(">I", data[: len(data) // 4 * 4])]


def collect(owner: str, target: bytes, obj: Object | None, gp: int | None) -> tuple[list[Reference], list[str]]:
    code = words(target)
    result = (
        [
            Reference(owner, r.address, r.type, r.offset, code[r.offset // 4] >> 26, "absolute memory")
            for r in references(code, gp)
        ]
        if code
        else []
    )
    constants = {0: 0}
    reset = False
    for index, word in enumerate(code):
        reset_now, reset = reset, False
        op, rs, rt = word >> 26, word >> 21 & 31, word >> 16 & 31
        if op == 15 and rt:
            constants[rt] = (word & 65535) << 16
        elif op in (9, 13) and rt:
            if rs in constants:
                value = (constants[rs] + signed(word) if op == 9 else constants[rs] | (word & 65535)) & 0xFFFFFFFF
                constants[rt] = value
                if value >= 0x80000000:
                    result.append(Reference(owner, value, "address", index * 4, op, "constant address pair"))
            else:
                constants.pop(rt, None)
        elif op == 0 and word >> 11 & 31:
            constants.pop(word >> 11 & 31, None)
        elif op in (*range(8, 15), *range(32, 40)):
            constants.pop(rt, None)
        if op in (1, 2, 3, 4, 5, 6, 7, 20, 21, 22, 23) or (op == 0 and word & 63 in (8, 9)):
            reset = True
        if reset_now:
            constants = {0: 0}
    result.extend(
        Reference(owner, r.address, "indexed", r.offset, code[r.offset // 4] >> 26, "indexed anchor", scale=r.scale)
        for r in indexed_references(code)
    )
    errors: list[str] = []
    text = obj.section(".text") if obj is not None else None
    if obj is None or text is None:
        return result, []
    source = words(obj.content(text))
    groups: dict[tuple[str, int, int], list[Relocation]] = defaultdict(list)
    for at, kind, symbol in obj.relocations(text):
        key = symbol["name"], symbol["section"], symbol["value"]
        groups[key].append(Relocation(at, kind, symbol["name"] or f"@{symbol['section']}"))
    for key, rels in groups.items():
        try:
            pairs = relocation_pairs(rels)
        except Held as error:
            errors.append(f"{owner}: {error.reason}")
            continue
        for high, low in pairs:
            at = low.offset // 4
            if low.kind not in (2, 6, 7):
                continue
            if at >= min(len(source), len(code)) or source[at] & 0xFFFF0000 != code[at] & 0xFFFF0000:
                errors.append(f"{owner}: relocation instruction differs at {low.offset}")
                continue
            word = code[at]
            if high is not None:
                if high.offset // 4 >= len(code):
                    errors.append(f"{owner}: HI16 outside target")
                    continue
                address = (((code[high.offset // 4] & 0xFFFF) << 16) + signed(word)) & 0xFFFFFFFF
            elif low.kind == 2:
                address = word
            elif low.kind == 7 and gp is not None:
                address = (gp + signed(word)) & 0xFFFFFFFF
            else:
                continue
            type_ = {0x31: "f32", 0x35: "f64", 0x39: "f32", 0x3D: "f64"}.get(word >> 26, "address")
            result.append(Reference(owner, address, type_, low.offset, word >> 26, "target relocation", key[0]))
    return result, errors
