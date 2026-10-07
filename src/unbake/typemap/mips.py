"""Conservative MIPS III value provenance across control flow and delay slots."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field, replace
from typing import Any

# Width describes the access, not the semantic C storage type.
MEMORY = {
    0x20: (1, True, "read"),
    0x21: (2, True, "read"),
    0x22: (4, None, "read"),
    0x23: (4, True, "read"),
    0x24: (1, False, "read"),
    0x25: (2, False, "read"),
    0x26: (4, None, "read"),
    0x27: (4, False, "read"),
    0x28: (1, None, "write"),
    0x29: (2, None, "write"),
    0x2A: (4, None, "write"),
    0x2B: (4, None, "write"),
    0x2C: (8, None, "write"),
    0x2D: (8, None, "write"),
    0x2E: (4, None, "write"),
    0x30: (4, True, "read"),
    0x31: (4, None, "read"),
    0x34: (8, None, "read"),
    0x35: (8, None, "read"),
    0x37: (8, None, "read"),
    0x38: (4, None, "write"),
    0x39: (4, None, "write"),
    0x3C: (8, None, "write"),
    0x3D: (8, None, "write"),
    0x3F: (8, None, "write"),
    0x1A: (8, None, "read"),
    0x1B: (8, None, "read"),
}
ARGUMENTS = (4, 5, 6, 7, 44, 46)
RETURNS = (2, 3, 32, 34)


@dataclass(frozen=True)
class Value:
    origins: tuple[tuple[str, int], ...] = ()
    constant: int | None = None
    defined: bool = True
    dependencies: tuple[str, ...] = ()
    # Origins of a pointer this value was advanced from by an untracked index (offset unknown).
    based: tuple[str, ...] = ()
    types: tuple[str, ...] = ()

    def shift(self, offset: int) -> Value:
        if self.constant is not None:
            return Value(constant=(self.constant + offset) & 0xFFFFFFFF)
        return Value(
            tuple((name, delta + offset) for name, delta in self.origins),
            defined=self.defined,
            dependencies=self.dependencies,
            based=self.based,
            types=self.types,
        )

    def merge(self, other: Value) -> Value:
        if self == other:
            return self
        dependencies = tuple(sorted(set(self.dependencies + other.dependencies)))
        left_types = self.types or (("int",) if self.constant is not None else ())
        right_types = other.types or (("int",) if other.constant is not None else ())
        types = tuple(sorted(set(left_types + right_types))) if left_types and right_types else ()
        if not self.origins or not other.origins:
            based = tuple(sorted(set(self.based + other.based))) if not self.origins and not other.origins else ()
            return Value(defined=self.defined and other.defined, dependencies=dependencies, based=based, types=types)
        origins = tuple(sorted(set(self.origins + other.origins)))
        return (
            Value(origins, defined=self.defined and other.defined, dependencies=dependencies, types=types)
            if len(origins) <= 4
            else Value(defined=self.defined and other.defined, dependencies=dependencies, types=types)
        )

    def data(self) -> dict[str, Any]:
        return {
            "origins": [{"id": name, "offset": offset} for name, offset in self.origins],
            "constant": self.constant,
            "unknown": not self.origins and self.constant is None,
            "defined": self.defined,
            "dependencies": list(self.dependencies),
            "based": list(self.based),
            "types": list(self.types),
        }


def _advanced(left: Value, right: Value) -> Value:
    """LEFT + RIGHT when one is a plain tracked pointer (a single origin at offset 0) and the other is not:
    a counter or offset value (several origins, a shifted origin) or one with no origin at all."""

    def plain(value: Value) -> bool:
        return value.constant is None and len(value.origins) == 1 and value.origins[0][1] == 0

    for pointer, index in ((left, right), (right, left)):
        if plain(pointer) and not plain(index) and index.constant is None:
            return Value(defined=pointer.defined, dependencies=pointer.dependencies, based=(pointer.origins[0][0],))
    return UNKNOWN


UNKNOWN = Value(defined=False)
ZERO = Value(constant=0)


def register(index: int) -> str:
    return f"r{index}" if index < 32 else f"f{index - 32}"


@dataclass
class State:
    registers: list[Value]
    stack: dict[tuple[int, int], Value] = field(default_factory=dict)

    def copy(self) -> State:
        return State(self.registers.copy(), self.stack.copy())

    def merge(self, other: State) -> State:
        return State(
            [left.merge(right) for left, right in zip(self.registers, other.registers, strict=True)],
            {key: value.merge(other.stack.get(key, UNKNOWN)) for key, value in self.stack.items()},
        )


def control(word: int, pc: int) -> tuple[str, int | None, bool] | None:
    op, rs, rt, fn = word >> 26, word >> 21 & 31, word >> 16 & 31, word & 63
    if op in (2, 3):
        return ("call" if op == 3 else "jump", ((pc + 4) & 0xF0000000) | ((word & 0x3FFFFFF) << 2), False)
    if op == 0 and fn in (8, 9):
        return ("call" if fn == 9 else "return" if rs == 31 else "jump", None, False)
    if op in (1, 4, 5, 6, 7, 20, 21, 22, 23) or (op == 17 and rs == 8):
        offset = (word & 0x7FFF) - (word & 0x8000)
        return (
            "call" if op == 1 and rt in (16, 17, 18, 19) else "branch",
            pc + 4 + offset * 4,
            op in (20, 21, 22, 23) or (op == 1 and rt in (2, 3, 18, 19)) or (op == 17 and bool(rt & 2)),
        )
    return None


class Analysis:
    def __init__(
        self,
        function: str,
        version: str,
        address: int,
        rom_offset: int,
        words: list[int],
        targets: dict[int, str],
        symbols: dict[int, list[str]],
    ) -> None:
        self.function, self.version, self.address, self.rom_offset = function, version, address, rom_offset
        self.words, self.targets, self.symbols = words, targets, symbols
        self.memory: dict[int, dict[str, Any]] = {}
        self.calls: dict[int, dict[str, Any]] = {}
        self.returns: dict[int, dict[str, Any]] = {}
        self.inputs: set[str] = set()
        self.outputs: set[str] = set()
        self.uses: dict[str, set[int]] = {}
        self.unknown: set[str] = set()
        self.value_types: dict[tuple[str, str], dict[str, Any]] = {}

    def provenance(self, index: int) -> dict[str, Any]:
        return {
            "function": self.function,
            "version": self.version,
            "instruction": self.address + index * 4,
            "rom_offset": self.rom_offset + index * 4,
        }

    def use(self, value: Value, index: int, record: bool) -> None:
        if record:
            for origin, _ in value.origins:
                if origin.startswith(f"param:{self.function}:"):
                    self.inputs.add(origin.rsplit(":", 1)[1])
                if origin.startswith("return:"):
                    self.uses.setdefault(origin, set()).add(self.address + index * 4)

    def hint(self, value: Value, type_: str, index: int, record: bool) -> None:
        if record and len(value.origins) == 1 and value.origins[0][1] == 0:
            node = value.origins[0][0]
            self.value_types[node, type_] = {"node": node, "type": type_, **self.provenance(index)}

    def step(self, state: State, index: int, record: bool) -> None:
        word = self.words[index]
        op, rs, rt, rd, fn = word >> 26, word >> 21 & 31, word >> 16 & 31, word >> 11 & 31, word & 63
        immediate = (word & 0x7FFF) - (word & 0x8000)
        regs = state.registers
        rs_constant, rt_constant = regs[rs].constant, regs[rt].constant
        destination: int | None = None
        value = UNKNOWN
        read: list[int] = []
        if op in MEMORY:
            width, signedness, direction = MEMORY[op]
            read = [rs]
            operand = rt + 32 if op in (0x31, 0x35, 0x39, 0x3D) else rt
            base = regs[rs].shift(immediate)
            slot = next((offset for origin, offset in base.origins if origin == f"stack:{self.function}"), None)
            if record:
                self.memory[index] = {
                    **self.provenance(index),
                    "opcode": op,
                    "base_register": register(rs),
                    "base": regs[rs].data(),
                    "offset": immediate,
                    "width": width,
                    "signedness": signedness,
                    "direction": direction,
                    "value": regs[operand].data() if direction == "write" else None,
                    "partial": op in (0x22, 0x26, 0x2A, 0x2E, 0x1A, 0x1B, 0x2C, 0x2D),
                }
            if direction == "read":
                destination = operand
                if slot is not None and len(base.origins) == 1:
                    value = state.stack.get((slot, width), UNKNOWN)
                    if value == UNKNOWN and slot >= 16:
                        value = Value(((f"param:{self.function}:stack{slot}", 0),))
                        # A speculative/dead load does not consume a formal.
                        # Its provenance becomes an input when the loaded
                        # value is read, forwarded to a consuming callee, or
                        # reaches a possible result register below.
                elif base.constant is not None and len(self.symbols.get(base.constant, [])) == 1:
                    value = Value((("global:" + self.symbols[base.constant][0], 0),))
                else:
                    value = Value(((f"memory:{self.function}:{self.version}:{index}", 0),))
                if op in (0x31, 0x35):
                    type_ = "float" if width == 4 else "double"
                    self.hint(value, type_, index, record)
                    value = replace(value, types=(type_,))
                if record:
                    self.memory[index]["loaded"] = value.data()
                if width == 8 and operand >= 32 and operand + 1 < 64:
                    regs[operand + 1] = UNKNOWN
            else:
                read.append(operand)
                if op in (0x39, 0x3D) and not regs[operand].types:
                    self.hint(regs[operand], "float" if width == 4 else "double", index, record)
                if slot is not None and len(base.origins) == 1:
                    # A write invalidates every overlapping spill, irrespective of width.
                    state.stack = {
                        key: old
                        for key, old in state.stack.items()
                        if key[0] + key[1] <= slot or slot + width <= key[0]
                    }
                    state.stack[slot, width] = regs[operand]
                if op in (0x38, 0x3C):
                    destination = rt
        elif op == 15:
            destination, value = rt, Value(constant=(word & 65535) << 16)
        elif op in (9, 0x19):
            read, destination, value = [rs], rt, regs[rs].shift(immediate)
        elif op in (8, 10, 11, 12, 13, 14, 0x18):
            read, destination = [rs], rt
            if op == 13 and rs_constant is not None:
                value = Value(constant=rs_constant | (word & 65535))
        elif op == 0:
            if fn in (0x20, 0x21, 0x2C, 0x2D, 0x25):
                read, destination = [rs, rt], rd
                if regs[rt] == ZERO:
                    value = regs[rs]
                elif regs[rs] == ZERO:
                    value = regs[rt]
                elif fn in (0x20, 0x21, 0x2C, 0x2D) and rt_constant is not None:
                    value = regs[rs].shift(rt_constant)
                elif fn in (0x20, 0x21, 0x2C, 0x2D) and rs_constant is not None:
                    value = regs[rt].shift(rs_constant)
                elif fn in (0x20, 0x21):
                    value = _advanced(regs[rs], regs[rt])
            elif fn in (0, 2, 3, 0x38, 0x3A, 0x3B, 0x3C, 0x3E, 0x3F):
                read, destination = [rt], rd
                if word >> 6 & 31 == 0 and fn == 0:
                    value = regs[rt]
                elif fn == 0 and rt_constant is not None:
                    value = Value(constant=(rt_constant << (word >> 6 & 31)) & 0xFFFFFFFF)
            elif fn in (8, 9):
                read = [rs]
                destination = rd if fn == 9 else None
            elif fn in (0x10, 0x12):
                destination = rd
            elif fn in (0x11, 0x13, 0x18, 0x19, 0x1A, 0x1B, 0x1C, 0x1D, 0x1E, 0x1F):
                read = [rs, rt]
            elif fn not in (12, 13, 15):
                read, destination = [rs, rt], rd
        elif op == 16 and rs in (0, 2):
            # A word transfer from a CPU control register defines its GPR
            # destination even though the control value has unknown semantics.
            destination, value = rt, Value()
        elif op == 16 and rs in (4, 6):
            read = [rt]
        elif op == 17:
            if rs in (0, 1, 2):
                read, destination = [rd + 32], rt
                value = regs[rd + 32] if rs == 0 else UNKNOWN
            elif rs in (4, 5, 6):
                read, destination = [rt], rd + 32
                value = regs[rt] if rs == 4 else UNKNOWN
            elif rs >= 16:
                read, destination = [rd + 32, rt + 32], (word >> 6 & 31) + 32
                if fn in (*range(4, 16), *range(0x20, 0x26)):
                    read = [rd + 32]
                if fn == 6:
                    value = regs[rd + 32]
                if fn >= 0x30:
                    destination = None
        elif op in (1, 4, 5, 6, 7, 20, 21, 22, 23):
            read = [rs, rt] if op in (4, 5, 20, 21) else [rs]
        elif op not in (2, 3, 0x2F):
            if record:
                self.unknown.add(f"instruction 0x{self.address + index * 4:X}: unsupported opcode 0x{op:X}")
            # Unsupported coprocessor effects cannot retain stale register identities.
            destination = rt
        # Record value semantics independently of memory storage width. Copies
        # preserve provenance; actual arithmetic produces a distinct value node.
        if (
            destination is not None
            and value == UNKNOWN
            and (
                (op == 0 and fn not in (8, 9, 12, 13, 15))
                or op in (8, 10, 11, 12, 13, 14, 0x18)
                or (op == 17 and rs >= 16 and fn < 0x30)
            )
        ):
            output_type = "int"
            if op == 17 and rs >= 16:
                output_type = "double" if (fn == 0x21 or (rs == 17 and fn not in (0x20, 0x24, 0x25))) else "float"
                if fn in (0x24, 0x25, 0x0C, 0x0D, 0x0E, 0x0F):
                    output_type = "int"
            value = Value(
                ((f"value:{self.function}:{self.version}:{index}", 0),),
                types=(output_type,),
                defined=all(regs[item].defined for item in read),
                dependencies=tuple(sorted({dependency for item in read for dependency in regs[item].dependencies})),
            )
            self.hint(value, output_type, index, record)
        if op == 17 and rs >= 16:
            input_type = {16: "float", 17: "double", 20: "int", 21: "long long"}.get(rs)
            if input_type:
                self.hint(regs[rd + 32], input_type, index, record)
                if fn in (0, 1, 2, 3) or fn >= 0x30:
                    self.hint(regs[rt + 32], input_type, index, record)
        if (op == 0 and fn in (0x20, 0x22, 0x24, 0x25, 0x26, 0x27, 0x2A, 0x18, 0x1A)) or op in (8, 10, 12, 14):
            for item in read:
                self.hint(regs[item], "int", index, record)
        elif op == 0 and fn in (0x2B, 0x19, 0x1B):
            for item in read:
                self.hint(regs[item], "unsigned int", index, record)
        for item in read:
            self.use(regs[item], index, record)
        if destination:
            regs[destination] = value
            if record:
                self.outputs.add(register(destination))
        regs[0] = ZERO

    def block(self, incoming: State, start: int, record: bool) -> list[tuple[int, State]]:
        state = incoming.copy()
        index = start
        while index < len(self.words):
            pc = self.address + index * 4
            branch = control(self.words[index], pc)
            if branch is None:
                self.step(state, index, record)
                index += 1
                if index in self.leaders:
                    return [(index, state)]
                continue
            kind, target, likely = branch
            rs = self.words[index] >> 21 & 31
            if target is None and kind in ("call", "jump"):
                target = state.registers[rs].constant
            self.step(state, index, record)
            unslotted = state.copy()
            if index + 1 < len(self.words):
                self.step(state, index + 1, record)
            elif record:
                self.unknown.add(f"instruction 0x{pc:X}: missing delay slot")
            next_index = index + 2
            if kind == "return":
                if record:
                    for r in RETURNS:
                        for origin, _ in state.registers[r].origins:
                            if origin.startswith(f"param:{self.function}:stack"):
                                self.inputs.add(origin.rsplit(":", 1)[1])
                    self.returns[index] = {
                        **self.provenance(index),
                        "values": {register(r): state.registers[r].data() for r in RETURNS},
                    }
                return []
            callee = self.targets.get(target) if target is not None else None
            tail = kind == "jump" and callee is not None
            if kind == "call" or tail:
                if record:
                    self.calls[index] = {
                        **self.provenance(index),
                        "target": target,
                        "callee": callee,
                        "tail": tail,
                        "arguments": {
                            **{register(r): state.registers[r].data() for r in ARGUMENTS},
                            **{
                                f"stack{offset - sp_offset}": value.data()
                                for (offset, width), value in state.stack.items()
                                for origin, sp_offset in state.registers[29].origins
                                if origin == f"stack:{self.function}" and offset - sp_offset >= 16 and width in (4, 8)
                            },
                        },
                        "return_use": [],
                    }
                if tail:
                    return []
                for r in (*range(1, 16), 24, 25, *range(32, 52)):
                    state.registers[r] = UNKNOWN
                for r in RETURNS:
                    origin = f"return:{self.function}:{self.version}:{index}:{register(r)}"
                    state.registers[r] = Value(((origin, 0),), dependencies=(origin,))
                return [(next_index, state)] if next_index < len(self.words) else []
            successors = []
            if target is not None and self.address <= target < self.address + len(self.words) * 4:
                successors.append(((target - self.address) // 4, state))
            elif record:
                self.unknown.add(f"instruction 0x{pc:X}: unresolved control target {target}")
            if kind == "branch" and next_index < len(self.words):
                successors.append((next_index, unslotted if likely else state))
            return successors
        return []

    def run(self) -> dict[str, Any]:
        self.leaders = {0}
        for index, word in enumerate(self.words):
            branch = control(word, self.address + index * 4)
            if branch is not None:
                target = branch[1]
                self.leaders.add(index + 2)
                if target is not None and self.address <= target < self.address + len(self.words) * 4:
                    self.leaders.add((target - self.address) // 4)
        registers = [Value(((f"param:{self.function}:{register(r)}", 0),), defined=r not in RETURNS) for r in range(64)]
        registers[0], registers[29] = ZERO, Value(((f"stack:{self.function}", 0),))
        states = {0: State(registers)}
        pending = deque([0])
        while pending:
            start = pending.popleft()
            for successor, state in self.block(states[start], start, False):
                previous = states.get(successor)
                merged = previous.merge(state) if previous is not None else state
                if previous != merged:
                    states[successor] = merged
                    pending.append(successor)
        for start, state in sorted(states.items()):
            self.block(state, start, True)
        # Preserve base+offset facts even for unreachable/indirect-target blocks.
        covered = set(self.memory)
        for index, word in enumerate(self.words):
            if word >> 26 in MEMORY and index not in covered:
                self.step(State([ZERO, *([UNKNOWN] * 63)]), index, True)
        for index, call in self.calls.items():
            call["return_register_use"] = {
                register(r): sorted(
                    self.uses.get(f"return:{self.function}:{self.version}:{index}:{register(r)}", set())
                )
                for r in RETURNS
            }
            call["return_use"] = sorted(
                set().union(
                    *(
                        self.uses.get(f"return:{self.function}:{self.version}:{index}:{register(r)}", set())
                        for r in RETURNS
                    )
                )
            )
        return {
            "register_inputs": sorted(self.inputs),
            "register_outputs": sorted(self.outputs),
            "value_types": list(self.value_types.values()),
            "calls": list(self.calls.values()),
            "returns": list(self.returns.values()),
            "memory": [self.memory[i] for i in sorted(self.memory)],
            "unknown": sorted(self.unknown),
        }
